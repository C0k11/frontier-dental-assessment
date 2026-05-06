"""Extractor: produces a complete Product from one detail page.

Three-phase strategy:

  Phase 1 — Selector pass (rule-based, fast, deterministic).
            Pulls structural fields: name, sku, brand, price, image,
            availability, description.

  Phase 2 — LLM fallback for missing structural fields.
            Fires only when Phase 1 leaves critical fields empty
            (sku / brand / price / description). Asks the LLM to fill
            just those fields from the same HTML.

  Phase 3 — LLM enrichment of semantic specs (always-on if enabled).
            Reads the cleaned description and extracts structured
            attributes (material, color, sterile, form, intended_use,
            …) that selectors fundamentally cannot parse from prose.
            This is where the LLM earns its keep on a clean Magento
            site like Safco — Phase 2 rarely fires, but Phase 3 fires
            on every product with a non-trivial description.

Why this design:
  - Selectors are 100x cheaper and 10x faster than LLM calls — use them
    wherever a deterministic rule is possible (Phase 1).
  - LLM fallback is a safety net for site changes / irregular layouts
    (Phase 2) — bounded by `max_calls_per_run`.
  - LLM enrichment turns free-text descriptions into queryable
    attributes (Phase 3) — the kind of data that powers downstream
    e-commerce search and filtering.
  - Audit trail (`extraction_method`, `fields_via_llm`) lets us monitor
    whether each phase is doing its job.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urljoin

from loguru import logger
from selectolax.parser import HTMLParser, Node


# Common page-builder CSS that leaks into description text on Magento Hyva
_CSS_NOISE_RE = re.compile(r"#html-body\s*\[[^\]]*\]\s*\{[^}]*\}", re.DOTALL)


def _clean_description(text: Optional[str]) -> Optional[str]:
    """Strip page-builder CSS-in-text artifacts and leading 'Description' label."""
    if not text:
        return text
    text = _CSS_NOISE_RE.sub(" ", text)
    # Some pages emit "Description" as the heading inside the same element.
    text = re.sub(r"^Description\s*", "", text, count=1)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text or None

from ..config import DetailSelectors
from ..llm import LLMClient
from ..models import ExtractionMethod, Product


DEFAULT_ENRICHMENT_ATTRS = [
    "material", "color", "powder_free", "size", "pack_size",
    "sterile", "absorbable", "form", "texture", "intended_use",
    "latex_free", "thickness_mil",
]


class ExtractorAgent:
    def __init__(
        self,
        selectors: DetailSelectors,
        llm: LLMClient,
        enable_llm_fallback: bool = True,
        enable_enrichment: bool = True,
        enrichment_attributes: Optional[list[str]] = None,
    ):
        self.sel = selectors
        self.llm = llm
        self.enable_llm = enable_llm_fallback
        self.enable_enrichment = enable_enrichment
        self.enrichment_attributes = enrichment_attributes or DEFAULT_ENRICHMENT_ATTRS

    async def extract(
        self,
        url: str,
        html: str,
        category_path: Optional[list[str]] = None,
    ) -> Optional[Product]:
        """Extract one Product from rendered HTML. Returns None if name unfound."""
        tree = HTMLParser(html)

        # === Step 1: rule-based extraction ===
        # SKU on Safco is in form[data-sku] attribute, not text content —
        # try attribute first, then text fallback.
        sku = self._first_attr(tree, self.sel.sku, "data-sku") or self._first_text(tree, self.sel.sku)

        data: dict[str, Any] = {
            "url": url,
            "name": self._first_text(tree, self.sel.name),
            "brand": self._first_text(tree, self.sel.brand),
            "sku": sku,
            "price": self._first_text(tree, self.sel.price),
            "description": _clean_description(self._first_text(tree, self.sel.description)),
            "pack_size": self._first_text(tree, self.sel.pack_size),
            # Availability: only trust Schema.org href; ignore garbled button text.
            "availability": self._availability(tree, self.sel.availability),
            "image_urls": self._image_urls(tree, self.sel.images, base_url=url),
            "specifications": self._spec_table(tree, self.sel.specifications_rows),
            "category_path": category_path or self._breadcrumbs(tree, self.sel.breadcrumbs),
        }

        # If we can't even find the name, this is probably not a product page.
        if not data["name"]:
            logger.warning(f"No product name found at {url} — skipping")
            return None

        # === Step 2: identify gaps ===
        try:
            # Construct preliminary product to check missing fields
            # (don't enforce the full schema yet — partial data ok at this stage)
            prelim = Product(**{k: v for k, v in data.items() if v is not None})
        except Exception as e:
            logger.warning(f"Pydantic rejected initial extraction at {url}: {e}")
            prelim = Product(name=data["name"], url=url)

        missing = prelim.critical_fields_missing()
        method = ExtractionMethod.SELECTOR
        fields_via_llm: list[str] = []

        # === Step 3: LLM fallback if gaps + enabled ===
        if missing and self.enable_llm:
            logger.info(f"Selector miss for {missing} at {url} → invoking LLM fallback")
            try:
                llm_data = await self.llm.extract_product_fields(html, missing)
                for field in missing:
                    if field in llm_data and llm_data[field]:
                        data[field] = llm_data[field]
                        fields_via_llm.append(field)
                if fields_via_llm:
                    method = ExtractionMethod.HYBRID
            except Exception as e:
                logger.warning(f"LLM fallback failed at {url}: {e}")

        # If selectors got nothing useful and LLM filled everything, mark as LLM
        if fields_via_llm and method == ExtractionMethod.HYBRID:
            selector_fields = [k for k in data if data[k] and k not in fields_via_llm and k not in ("url", "category_path")]
            if len(selector_fields) <= 1:
                method = ExtractionMethod.LLM_FALLBACK

        # === Step 4: build initial Product ===
        confidence = self._confidence(method, fields_via_llm, data)
        try:
            product = Product(
                **{k: v for k, v in data.items() if v is not None},
                extraction_method=method,
                extraction_confidence=confidence,
                fields_via_llm=fields_via_llm,
            )
        except Exception as e:
            logger.error(f"Final Pydantic validation failed at {url}: {e}")
            return None

        # === Step 5: LLM enrichment of semantic attributes ===
        # This is where the LLM most reliably earns its keep on a clean
        # site: parsing free-text description into structured specs that
        # selectors cannot reach.
        if self.enable_enrichment:
            await self._enrich_specifications(product)

        return product

    async def _enrich_specifications(self, product: Product) -> None:
        """Extract structured attributes from product description with the LLM.

        Mutates `product.specifications` in place. Existing keys (set by
        selectors) are preserved — selector-derived specs win.
        """
        if not product.description and not product.name:
            return
        try:
            extracted = await self.llm.extract_attributes(
                name=product.name,
                description=product.description or "",
                attributes=self.enrichment_attributes,
            )
        except Exception as e:
            logger.warning(f"Enrichment LLM call failed for {product.sku or product.url}: {e}")
            return

        if not extracted:
            return

        added: list[str] = []
        for k, v in extracted.items():
            if v is None or v == "":
                continue
            if k in product.specifications:
                # Selector-derived value wins
                continue
            product.specifications[k] = str(v)
            added.append(k)

        if added:
            if "specifications" not in product.fields_via_llm:
                product.fields_via_llm.append("specifications")
            if product.extraction_method == ExtractionMethod.SELECTOR:
                product.extraction_method = ExtractionMethod.HYBRID
                product.extraction_confidence = min(product.extraction_confidence, 0.85)
            logger.debug(f"Enriched {product.sku or product.url} with {added}")

    # === Helpers ===

    def _first_text(self, tree: HTMLParser, combined_selector: str) -> Optional[str]:
        """Try each comma-separated selector; return first non-empty trimmed text."""
        for sel in self._split(combined_selector):
            node = tree.css_first(sel)
            if node:
                txt = node.text(strip=True)
                if txt:
                    return txt
        return None

    def _first_attr(self, tree: HTMLParser, combined_selector: str, attr: str) -> Optional[str]:
        """Try each comma-separated selector; return first non-empty value of `attr`."""
        for sel in self._split(combined_selector):
            node = tree.css_first(sel)
            if node:
                val = node.attributes.get(attr)
                if val:
                    return val.strip()
        return None

    def _first_attr_or_text(self, tree: HTMLParser, combined_selector: str) -> Optional[str]:
        """For availability — check itemprop content attr first, fall back to text."""
        for sel in self._split(combined_selector):
            node = tree.css_first(sel)
            if node:
                # Schema.org availability is in href attr (e.g., "http://schema.org/InStock")
                href = node.attributes.get("href")
                if href:
                    if "InStock" in href:
                        return "in_stock"
                    if "OutOfStock" in href:
                        return "out_of_stock"
                txt = node.text(strip=True)
                if txt:
                    return txt
        return None

    def _availability(self, tree: HTMLParser, combined_selector: str) -> Optional[str]:
        """Conservative availability: only trust Schema.org `[itemprop=availability]`
        href and explicit class signals — ignore button-label text noise."""
        for sel in self._split(combined_selector):
            for node in tree.css(sel):
                href = node.attributes.get("href") or ""
                if "InStock" in href:
                    return "in_stock"
                if "OutOfStock" in href:
                    return "out_of_stock"
                if "BackOrder" in href or "Backorder" in href:
                    return "backorder"
                cls = node.attributes.get("class") or ""
                if "stock available" in cls or "available" in cls.split():
                    return "in_stock"
                if "stock unavailable" in cls:
                    return "out_of_stock"
        return None

    def _image_urls(self, tree: HTMLParser, combined_selector: str, base_url: str) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for sel in self._split(combined_selector):
            for node in tree.css(sel):
                src = (
                    node.attributes.get("src")
                    or node.attributes.get("data-src")
                    or node.attributes.get("data-lazy-src")
                )
                if not src:
                    continue
                absolute = urljoin(base_url, src)
                if absolute not in seen and absolute.startswith("http"):
                    seen.add(absolute)
                    urls.append(absolute)
        return urls[:10]  # cap to avoid runaway

    def _spec_table(self, tree: HTMLParser, combined_selector: str) -> dict[str, str]:
        specs: dict[str, str] = {}
        for sel in self._split(combined_selector):
            for row in tree.css(sel):
                # Expect <tr><th>Key</th><td>Value</td></tr> pattern
                cells = row.css("th, td")
                if len(cells) >= 2:
                    key = cells[0].text(strip=True)
                    value = cells[1].text(strip=True)
                    if key and value:
                        specs[key] = value
        return specs

    def _breadcrumbs(self, tree: HTMLParser, combined_selector: str) -> list[str]:
        for sel in self._split(combined_selector):
            items = tree.css(sel)
            if items:
                path = []
                for item in items:
                    txt = item.text(strip=True)
                    if txt and txt.lower() not in {"home"}:
                        path.append(txt)
                if path:
                    return path
        return []

    @staticmethod
    def _split(combined: str) -> list[str]:
        return [s.strip() for s in combined.split(",") if s.strip()]

    @staticmethod
    def _confidence(method: ExtractionMethod, llm_fields: list[str], data: dict) -> float:
        """Heuristic confidence score for monitoring."""
        if method == ExtractionMethod.SELECTOR:
            return 1.0
        if method == ExtractionMethod.HYBRID:
            return 0.8
        return 0.6  # pure LLM
