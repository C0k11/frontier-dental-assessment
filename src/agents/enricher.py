"""Enricher: LLM-powered post-extraction enrichment.

Where this fits in the pipeline:

    Selectors  →  structural fields (name, sku, price, brand, image)
                       │
                       ▼
    Enricher   →  semantic fields (specs / attributes from description)
                       │
                       ▼
    Validator  →  schema check + dedup

Why LLM here, not selectors or regex:

    Dental product descriptions are unstructured prose. A typical line
    reads:

        "Sterile, water-insoluble, malleable porcine gelatin
         absorbable sponge for hemostatic use in oral surgery."

    Extracting attributes like material=porcine_gelatin, sterile=true,
    absorbable=true, intended_use=hemostatic with rules would require
    hundreds of patterns per attribute and would still miss synonyms.
    A small LLM call per product produces structured attributes that
    downstream e-commerce search and filtering can rely on.

Cost / value trade-off:

    One LLM call per product. With Gemini 2.5 Flash on the free tier
    (1500 req/day, sub-second latency) this is comfortable for a POC
    and trivially cacheable in production by hashing (name, description).
    The audit trail (`fields_via_llm`, `extraction_method`) lets us
    measure how often the enricher actually adds value vs the cost.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from ..llm import LLMClient
from ..models import ExtractionMethod, Product


# Default attribute set for dental supply products. Extendable via config.
DEFAULT_ATTRIBUTES = [
    "material",
    "color",
    "powder_free",
    "size",
    "pack_size",
    "sterile",
    "absorbable",
    "form",
    "texture",
    "intended_use",
    "latex_free",
    "thickness_mil",
]


class EnricherAgent:
    """Adds structured attributes to a Product by reading its description with an LLM."""

    def __init__(
        self,
        llm: LLMClient,
        attributes: Optional[list[str]] = None,
        enabled: bool = True,
    ):
        self.llm = llm
        self.attributes = attributes or DEFAULT_ATTRIBUTES
        self.enabled = enabled

    async def enrich(self, product: Product) -> Product:
        """Mutate `product.specifications` in place with LLM-extracted attributes.

        Skipped silently if:
          - Enricher disabled in config
          - Product has no description and no name (nothing to read)
        Existing keys in `specifications` are NOT overwritten — selector-derived
        specs win over LLM-derived ones.
        """
        if not self.enabled:
            return product
        if not product.description and not product.name:
            return product

        try:
            extracted = await self.llm.extract_attributes(
                name=product.name,
                description=product.description or "",
                attributes=self.attributes,
            )
        except Exception as e:
            logger.warning(f"Enricher LLM call failed for {product.sku or product.url}: {e}")
            return product

        if not extracted:
            return product

        added: list[str] = []
        for k, v in extracted.items():
            if v is None or v == "":
                continue
            # Don't overwrite selector-derived specs
            if k in product.specifications:
                continue
            product.specifications[k] = str(v)
            added.append(k)

        if added:
            if "specifications" not in product.fields_via_llm:
                product.fields_via_llm.append("specifications")
            # Bump method to HYBRID since LLM contributed
            if product.extraction_method == ExtractionMethod.SELECTOR:
                product.extraction_method = ExtractionMethod.HYBRID
                # Slight confidence drop since some fields are LLM-inferred
                product.extraction_confidence = min(product.extraction_confidence, 0.85)
            logger.debug(f"Enricher added {added} to {product.sku or product.url}")

        return product
