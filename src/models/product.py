"""Canonical Product schema. This is the contract every output row must satisfy."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class ExtractionMethod(str, Enum):
    """How a product was extracted — used for quality monitoring."""
    SELECTOR = "selector"
    LLM_FALLBACK = "llm_fallback"
    HYBRID = "hybrid"  # selector got most fields, LLM filled the rest


class Product(BaseModel):
    """Normalized product record.

    Required minimum: name + url. Everything else is best-effort.
    `sku` is the dedup key when available; falls back to `url`.
    """

    # === Required ===
    name: str = Field(..., min_length=1, description="Product display name")
    url: str = Field(..., description="Canonical product detail URL")
    category_path: list[str] = Field(default_factory=list, description="Breadcrumb path")

    # === Highly desired ===
    brand: Optional[str] = Field(None, description="Manufacturer / brand name")
    sku: Optional[str] = Field(None, description="Vendor SKU / item number")

    # === Commerce ===
    price: Optional[float] = Field(None, ge=0)
    currency: str = Field(default="USD")
    pack_size: Optional[str] = None
    availability: Optional[str] = Field(
        None,
        description="Free-form: 'in_stock', 'out_of_stock', 'backorder', or raw text",
    )

    # === Content ===
    description: Optional[str] = None
    specifications: dict[str, str] = Field(default_factory=dict)
    image_urls: list[str] = Field(default_factory=list)
    alternative_skus: list[str] = Field(default_factory=list)

    # === Provenance / observability ===
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extraction_method: ExtractionMethod = ExtractionMethod.SELECTOR
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    fields_via_llm: list[str] = Field(
        default_factory=list,
        description="Names of fields that were filled by LLM fallback (audit trail)",
    )

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip_whitespace(cls, v):
        if isinstance(v, str):
            return v.strip() or None if cls.model_fields.get("description") else v.strip()
        return v

    @field_validator("price", mode="before")
    @classmethod
    def _parse_price(cls, v):
        """Accept '$12.99', '12.99 USD', etc."""
        if v is None or isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            s = v.strip().replace("$", "").replace(",", "").replace("USD", "").strip()
            if not s:
                return None
            try:
                return float(s.split()[0])
            except (ValueError, IndexError):
                return None
        return v

    def dedup_key(self) -> str:
        """Stable identifier for deduplication."""
        return self.sku if self.sku else self.url

    def is_minimally_complete(self) -> bool:
        """True iff this row has the minimum fields to be useful downstream."""
        return bool(self.name and self.url)

    def critical_fields_missing(self) -> list[str]:
        """Used by extractor to decide whether to invoke LLM fallback."""
        missing = []
        if not self.sku:
            missing.append("sku")
        if not self.brand:
            missing.append("brand")
        if not self.description:
            missing.append("description")
        if not self.price:
            missing.append("price")
        return missing
