"""Async Playwright wrapper with retry, rate limit, and concurrency control.

Why Playwright: Safco Dental is Magento-based with JS-rendered product grids.
A static HTTP client would see the shell HTML but no products.

Why one shared browser context: cheaper than spawning a new browser per request.
Pages are still ephemeral (one page per fetch).
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_exponential,
)


@dataclass
class FetchResult:
    """Outcome of a single page fetch."""
    url: str
    final_url: str  # after redirects
    html: str
    status: int  # 200 if rendered ok, else error code
    duration_ms: int


class HttpClient:
    """Async context-managed Playwright client with politeness controls."""

    def __init__(
        self,
        *,
        delay_seconds: float = 2.0,
        max_concurrent: int = 2,
        timeout_ms: int = 30_000,
        retries: int = 3,
        retry_initial_wait: float = 1.5,
        user_agent: str = "Mozilla/5.0 (compatible; FrontierScraper/0.1)",
        save_raw_html_dir: Optional[Path] = None,
    ):
        self._delay = delay_seconds
        self._sem = asyncio.Semaphore(max_concurrent)
        self._timeout = timeout_ms
        self._retries = retries
        self._retry_initial_wait = retry_initial_wait
        self._user_agent = user_agent
        self._save_raw = save_raw_html_dir
        if self._save_raw:
            self._save_raw.mkdir(parents=True, exist_ok=True)

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._last_fetch_at = 0.0  # monotonic seconds

    async def __aenter__(self) -> "HttpClient":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=self._user_agent,
            viewport={"width": 1280, "height": 800},
        )
        logger.info("Playwright Chromium started")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("Playwright Chromium stopped")

    async def _politeness_wait(self) -> None:
        """Enforce minimum delay between requests (rough rate limit)."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_fetch_at
        if elapsed < self._delay:
            await asyncio.sleep(self._delay - elapsed)
        self._last_fetch_at = asyncio.get_event_loop().time()

    async def fetch(
        self,
        url: str,
        *,
        wait_for_selector: Optional[str] = None,
        wait_for_timeout_ms: Optional[int] = None,
    ) -> FetchResult:
        """Fetch one URL with retry. Returns rendered HTML.

        wait_for_selector: optional CSS to wait for before reading content
            (signals the JS-rendered content has actually loaded).
        """
        async with self._sem:
            await self._politeness_wait()

            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(self._retries),
                    wait=wait_exponential(multiplier=self._retry_initial_wait, min=1, max=20),
                    reraise=True,
                ):
                    with attempt:
                        return await self._fetch_once(
                            url,
                            wait_for_selector=wait_for_selector,
                            wait_for_timeout_ms=wait_for_timeout_ms or self._timeout,
                        )
            except RetryError as e:
                logger.error(f"Failed after {self._retries} retries: {url} | {e}")
                raise
            # Unreachable, but mypy
            raise RuntimeError("unreachable")

    async def _fetch_once(
        self,
        url: str,
        *,
        wait_for_selector: Optional[str],
        wait_for_timeout_ms: int,
    ) -> FetchResult:
        assert self._context is not None, "HttpClient not entered"
        page: Page = await self._context.new_page()
        start = asyncio.get_event_loop().time()
        try:
            response = await page.goto(url, timeout=self._timeout, wait_until="domcontentloaded")
            status = response.status if response else 0

            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector, timeout=wait_for_timeout_ms)
                except Exception as e:
                    logger.warning(f"wait_for_selector '{wait_for_selector}' timeout on {url}: {e}")

            html = await page.content()
            final_url = page.url
            duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)

            if self._save_raw:
                fname = hashlib.sha1(url.encode()).hexdigest()[:16] + ".html"
                (self._save_raw / fname).write_text(html, encoding="utf-8")

            logger.debug(f"[{status}] {url} ({duration_ms}ms)")
            return FetchResult(url=url, final_url=final_url, html=html, status=status, duration_ms=duration_ms)
        finally:
            await page.close()
