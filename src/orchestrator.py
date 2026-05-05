"""Orchestrator: ties all agents together into one run.

Design notes:
  - All I/O is async — Playwright + Gemini API + file writes are concurrent
  - Concurrency is bounded by HttpClient's semaphore + per-run product cap
  - Resumable: every successful URL is checkpointed; rerun skips done URLs
  - Idempotent: dedup by SKU (or URL) ensures rerunning produces same set
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

from .agents import (
    ExtractorAgent,
    NavigatorAgent,
    PageClassifierAgent,
    SelectorRepairAgent,
    ValidatorAgent,
)
from .config import Config
from .http import HttpClient
from .llm import build_llm_client
from .storage import CheckpointStore, JsonlWriter, csv_export


class Orchestrator:
    def __init__(self, config: Config):
        self.config = config

        self.http = HttpClient(
            delay_seconds=config.crawler.delay_seconds_between_requests,
            max_concurrent=config.crawler.max_concurrent_pages,
            timeout_ms=config.crawler.page_timeout_ms,
            retries=config.crawler.retries,
            retry_initial_wait=config.crawler.retry_initial_wait_seconds,
            user_agent=config.crawler.user_agent,
            save_raw_html_dir=Path(config.output.raw_html_dir) if config.output.save_raw_html else None,
        )

        self.llm = build_llm_client(
            provider=config.llm.provider,
            model=config.llm.model,
            max_calls=config.llm.max_calls_per_run,
            html_truncate=config.llm.html_truncate_chars,
        )

        self.classifier = PageClassifierAgent(
            llm=self.llm,
            enable_llm_fallback=config.llm.enable_classifier_fallback,
        )
        self.extractor = ExtractorAgent(
            selectors=config.selectors.detail,
            llm=self.llm,
            enable_llm_fallback=config.llm.enable_extractor_fallback,
        )
        self.validator = ValidatorAgent()
        self.repair = SelectorRepairAgent(
            llm=self.llm,
            enabled=config.llm.enable_selector_repair,
        )

        self.checkpoint = CheckpointStore(config.checkpoint.path)
        self.writer = JsonlWriter(config.output.jsonl_path)

    async def run(self) -> dict:
        async with self.http:
            for category in self.config.categories:
                await self._process_category(
                    category_url=category.url,
                    category_name=category.name,
                )

        # Export CSV view
        rows = csv_export(self.config.output.jsonl_path, self.config.output.csv_path)
        logger.info(f"Exported {rows} rows to CSV: {self.config.output.csv_path}")

        summary = {
            "validator": self.validator.summary(),
            "llm_calls": self.llm.call_count,
            "checkpoints": len(self.checkpoint),
            "csv_rows": rows,
        }
        logger.info(f"Run complete: {summary}")
        return summary

    async def _process_category(self, *, category_url: str, category_name: str) -> None:
        logger.info(f"=== Category: {category_name} ===")

        navigator = NavigatorAgent(
            http=self.http,
            listing_selectors=self.config.selectors.listing,
            max_products=self.config.crawler.max_products_per_category,
        )

        async for product_url in navigator.discover_products(category_url):
            if self.checkpoint.is_seen(product_url):
                logger.debug(f"Skipping already-seen: {product_url}")
                continue

            try:
                result = await self.http.fetch(product_url)
            except Exception as e:
                logger.error(f"Fetch failed for {product_url}: {e}")
                continue

            try:
                product = await self.extractor.extract(
                    url=product_url,
                    html=result.html,
                    category_path=[category_name],
                )
            except Exception as e:
                logger.error(f"Extract failed for {product_url}: {e}")
                continue

            if product is None:
                continue

            v = self.validator.validate(product)
            if v.valid:
                self.writer.append(product)
                logger.info(
                    f"+ {product.sku or '?'} | {product.name[:60]} "
                    f"({product.extraction_method.value})"
                )
            else:
                logger.debug(f"  rejected ({v.reason}): {product.name[:60]}")

            self.checkpoint.mark_seen(product_url)
