"""Page classifier: decides what kind of page we're on.

Hybrid: rule-based first (fast, deterministic), LLM fallback for edge cases.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from loguru import logger
from selectolax.parser import HTMLParser

from ..llm import LLMClient


class PageType(str, Enum):
    CATEGORY_LISTING = "category_listing"
    PRODUCT_DETAIL = "product_detail"
    OTHER = "other"


class PageClassifierAgent:
    """Hybrid rule + LLM page-type classifier."""

    def __init__(
        self,
        llm: LLMClient,
        listing_signal_selector: str = ".product-items, .products.list",
        detail_signal_selector: str = "[itemprop='sku'], [itemprop='price']",
        enable_llm_fallback: bool = True,
    ):
        self.llm = llm
        self.listing_signal = listing_signal_selector
        self.detail_signal = detail_signal_selector
        self.enable_llm = enable_llm_fallback

    async def classify(self, url: str, html: str) -> PageType:
        # Rule 1: URL pattern
        url_lower = url.lower()
        if "/catalog/" in url_lower and not url_lower.rstrip("/").endswith(".html"):
            # Catalog index pages typically end in slug, not .html
            page_type = self._dom_classify(html)
            if page_type != PageType.OTHER:
                return page_type
            return PageType.CATEGORY_LISTING

        # Rule 2: DOM signals
        dom_type = self._dom_classify(html)
        if dom_type != PageType.OTHER:
            return dom_type

        # Fallback: LLM
        if self.enable_llm:
            try:
                guess = await self.llm.classify_page(html)
                logger.debug(f"LLM classified {url} as {guess}")
                return PageType(guess) if guess in {p.value for p in PageType} else PageType.OTHER
            except Exception as e:
                logger.warning(f"LLM classification failed: {e}")

        return PageType.OTHER

    def _dom_classify(self, html: str) -> PageType:
        tree = HTMLParser(html)

        # Detail signal: presence of itemprop='sku' or '.price' AND single h1 product title
        for sel in self._split(self.detail_signal):
            if tree.css_first(sel):
                # Stronger check: detail page has single product H1
                if tree.css_first("h1.page-title, h1.product-title, h1[itemprop='name']"):
                    return PageType.PRODUCT_DETAIL

        # Listing signal: container with multiple product cards
        for sel in self._split(self.listing_signal):
            container = tree.css_first(sel)
            if container and len(tree.css(".product-item, [itemtype*='Product']")) >= 2:
                return PageType.CATEGORY_LISTING

        return PageType.OTHER

    @staticmethod
    def _split(combined: str) -> list[str]:
        return [s.strip() for s in combined.split(",") if s.strip()]
