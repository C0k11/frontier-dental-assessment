"""Navigator: discovers product URLs from category listing pages.

Pure rule-based - no LLM here. Categories follow predictable URL/DOM patterns
and using LLM would be slow + expensive for what amounts to anchor-tag
extraction.
"""
from __future__ import annotations

from typing import AsyncIterator
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

from loguru import logger
from selectolax.parser import HTMLParser

from ..config import ListingSelectors
from ..http import HttpClient


class NavigatorAgent:
    """Walks one category and yields product detail URLs."""

    def __init__(
        self,
        http: HttpClient,
        listing_selectors: ListingSelectors,
        max_products: int = 50,
    ):
        self.http = http
        self.sel = listing_selectors
        self.max_products = max_products

    async def discover_products(self, category_url: str) -> AsyncIterator[str]:
        """Yield product URLs from a category, walking pagination."""
        seen_pages: set[str] = set()
        current_url = category_url
        page_num = 1
        yielded = 0

        while current_url and current_url not in seen_pages:
            seen_pages.add(current_url)
            logger.info(f"Navigator: page {page_num} -> {current_url}")

            try:
                result = await self.http.fetch(
                    current_url,
                    wait_for_selector=self.sel.page_render_signal,
                )
            except Exception as e:
                logger.error(f"Navigator failed to fetch {current_url}: {e}")
                return

            tree = HTMLParser(result.html)
            product_links = self._extract_product_links(tree, current_url)

            if not product_links:
                logger.warning(f"No products found on {current_url} - possible selector drift")

            for link in product_links:
                if yielded >= self.max_products:
                    logger.info(f"Hit max_products cap ({self.max_products}); stopping navigator")
                    return
                yield link
                yielded += 1

            next_url = self._extract_next_page(tree, current_url)
            if not next_url or next_url == current_url:
                break
            current_url = next_url
            page_num += 1

    def _extract_product_links(self, tree: HTMLParser, base_url: str) -> list[str]:
        """Find all product detail URLs on a listing page.

        Algolia renders products into .ais-Hits-item containers. Each contains
        either an <a class="result" href="..."> or a <link itemprop="url" content="...">.
        """
        urls: list[str] = []
        seen: set[str] = set()

        for selector in self._split_selectors(self.sel.product_link):
            for node in tree.css(selector):
                # Prefer href, fall back to content (for <link itemprop="url">)
                href = node.attributes.get("href") or node.attributes.get("content")
                if not href:
                    continue
                absolute = urljoin(base_url, href)
                if self._looks_like_product_url(absolute) and absolute not in seen:
                    seen.add(absolute)
                    urls.append(absolute)
        return urls

    def _extract_next_page(self, tree: HTMLParser, base_url: str) -> str | None:
        """Find URL of the next listing page, if any."""
        for selector in self._split_selectors(self.sel.next_page):
            node = tree.css_first(selector)
            if node:
                href = node.attributes.get("href")
                if href:
                    return urljoin(base_url, href)
        return None

    @staticmethod
    def _split_selectors(combined: str) -> list[str]:
        """Selectors in config are comma-joined; split for retry on each."""
        return [s.strip() for s in combined.split(",") if s.strip()]

    @staticmethod
    def _looks_like_product_url(url: str) -> bool:
        """Heuristic filter for product detail URLs.
        Safco uses /product/<slug> (no .html). We accept these explicitly
        and exclude obvious non-product paths.
        """
        bad = (
            "/customer/", "/cart", "/checkout", "/search", "/contact",
            "/account", "/login", "javascript:", "#",
            "/shop-by-manufacturer/",  # brand pages, not products
            "/catalog",  # category pages
        )
        if any(b in url for b in bad):
            return False
        # Positive signal: /product/ path
        if "/product/" in url:
            return True
        # Fallback: legacy .html pages (some Magento sites)
        if url.endswith(".html"):
            return True
        return False
