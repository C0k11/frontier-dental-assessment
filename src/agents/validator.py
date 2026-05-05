"""Validator: schema check + dedup + business-rule gate.

Pydantic already enforces the schema in models/product.py. This agent layers
on:
  - dedup by SKU (or URL fallback)
  - business rules (must have name, url is the canonical detail page)
  - quality reasons (rejected reason logged for monitoring)
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from ..models import Product


@dataclass
class ValidationResult:
    valid: bool
    reason: str = "ok"


class ValidatorAgent:
    def __init__(self):
        self._seen_keys: set[str] = set()
        # Counters for run-end summary
        self.accepted = 0
        self.rejected_dup = 0
        self.rejected_incomplete = 0
        self.rejected_invalid = 0

    def validate(self, product: Product) -> ValidationResult:
        if not product.is_minimally_complete():
            self.rejected_incomplete += 1
            return ValidationResult(False, "incomplete: missing name or url")

        key = product.dedup_key()
        if key in self._seen_keys:
            self.rejected_dup += 1
            return ValidationResult(False, f"duplicate: {key}")
        self._seen_keys.add(key)

        # Business rule: detail URL should look like a real page
        if not product.url.startswith(("http://", "https://")):
            self.rejected_invalid += 1
            return ValidationResult(False, "invalid url scheme")

        self.accepted += 1
        return ValidationResult(True, "ok")

    def summary(self) -> dict:
        total = (
            self.accepted + self.rejected_dup + self.rejected_incomplete + self.rejected_invalid
        )
        return {
            "total_seen": total,
            "accepted": self.accepted,
            "rejected_duplicate": self.rejected_dup,
            "rejected_incomplete": self.rejected_incomplete,
            "rejected_invalid": self.rejected_invalid,
            "acceptance_rate": (self.accepted / total) if total else 0.0,
        }
