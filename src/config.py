"""Config loader. YAML → dataclasses-style typed access."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class CrawlerConfig(BaseModel):
    delay_seconds_between_requests: float = 2.0
    max_concurrent_pages: int = 2
    page_timeout_ms: int = 30000
    retries: int = 3
    retry_initial_wait_seconds: float = 1.5
    user_agent: str = "Mozilla/5.0 (compatible; FrontierScraper/0.1)"
    max_products_per_category: int = 50


class CategoryConfig(BaseModel):
    name: str
    slug: str
    url: str


class ListingSelectors(BaseModel):
    product_card: str
    product_link: str
    next_page: str
    page_render_signal: str


class DetailSelectors(BaseModel):
    name: str
    brand: str
    sku: str
    price: str
    description: str
    pack_size: str
    availability: str
    images: str
    specifications_rows: str
    breadcrumbs: str


class SelectorsConfig(BaseModel):
    listing: ListingSelectors
    detail: DetailSelectors


class OutputConfig(BaseModel):
    jsonl_path: str = "data/output/products.jsonl"
    csv_path: str = "data/output/products.csv"
    raw_html_dir: str = "data/raw_html"
    save_raw_html: bool = False


class CheckpointConfig(BaseModel):
    path: str = "data/checkpoints/seen.json"


class LLMConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-2.5-flash"
    enable_extractor_fallback: bool = True
    enable_classifier_fallback: bool = True
    enable_selector_repair: bool = True
    enable_enricher: bool = True
    enricher_attributes: list[str] = Field(
        default_factory=lambda: [
            "material", "color", "powder_free", "size", "pack_size",
            "sterile", "absorbable", "form", "texture", "intended_use",
            "latex_free", "thickness_mil",
        ]
    )
    max_calls_per_run: int = 250
    html_truncate_chars: int = 30000
    # Gemini free-tier RPM is 15. 4.5s gap = ~13 RPM with safety margin.
    min_seconds_between_calls: float = 4.5


class LoggingConfig(BaseModel):
    path: str = "logs/run.log"
    level: str = "INFO"


class Config(BaseModel):
    crawler: CrawlerConfig
    categories: list[CategoryConfig]
    selectors: SelectorsConfig
    output: OutputConfig
    checkpoint: CheckpointConfig
    llm: LLMConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str | Path) -> Config:
    """Load and validate config from YAML."""
    with open(path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    return Config(**raw)
