"""LLM client wrapper. Used selectively as fallback (NOT for primary extraction).

Why fallback-only: rule-based selectors are faster, cheaper, and more
deterministic. We invoke LLM only when:
  1. A page doesn't match any known layout (classification fallback)
  2. Selectors miss critical fields (extraction fallback)
  3. A selector breaks repeatedly across pages (selector repair)
"""
from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

from loguru import logger


class LLMClient(ABC):
    """Abstract LLM. Swap providers by implementing this."""

    @abstractmethod
    async def classify_page(self, html_excerpt: str) -> str:
        """Return one of: 'category_listing', 'product_detail', 'other'."""

    @abstractmethod
    async def extract_product_fields(
        self, html_excerpt: str, missing_fields: list[str]
    ) -> dict[str, Any]:
        """Return JSON dict with values for the requested missing fields.
        Omit fields not found - don't hallucinate.
        """

    @abstractmethod
    async def suggest_selectors(
        self, html_excerpt: str, broken_selector: str, target_field: str
    ) -> list[str]:
        """Return up to 3 candidate CSS selectors for `target_field`."""

    @abstractmethod
    async def extract_attributes(
        self, name: str, description: str, attributes: list[str]
    ) -> dict[str, str]:
        """Extract structured attributes from product description text.

        Used by EnricherAgent. Returns a dict of {attribute: value} where
        each attribute name comes from the requested list. Attributes for
        which no evidence is found are omitted (no hallucinations).
        """

    @property
    @abstractmethod
    def call_count(self) -> int:
        """Number of LLM calls made. Used for cost guardrails."""


class NullLLMClient(LLMClient):
    """No-op LLM. Used when GEMINI_API_KEY is not set or fallbacks are disabled."""

    async def classify_page(self, html_excerpt: str) -> str:
        return "other"

    async def extract_product_fields(
        self, html_excerpt: str, missing_fields: list[str]
    ) -> dict[str, Any]:
        return {}

    async def suggest_selectors(
        self, html_excerpt: str, broken_selector: str, target_field: str
    ) -> list[str]:
        return []

    async def extract_attributes(
        self, name: str, description: str, attributes: list[str]
    ) -> dict[str, str]:
        return {}

    @property
    def call_count(self) -> int:
        return 0


class GeminiClient(LLMClient):
    """Gemini 2.5 Flash. Cheap + fast + supports JSON mode."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.5-flash",
        max_calls: int = 50,
        html_truncate: int = 30_000,
        min_seconds_between_calls: float = 4.5,
    ):
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise ImportError(
                "google-generativeai required. Run: pip install google-generativeai"
            ) from e
        self._genai = genai

        api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY required. Set in .env or pass api_key directly."
            )
        genai.configure(api_key=api_key)
        self._model_name = model
        self._max_calls = max_calls
        self._html_truncate = html_truncate
        # Rate limit: Gemini free tier is 15 RPM. 4.5s gap = 13.3 RPM (safety).
        self._min_interval = min_seconds_between_calls
        self._last_call_at = 0.0
        self._calls = 0
        self._lock = asyncio.Lock()

    @property
    def call_count(self) -> int:
        return self._calls

    async def _generate_json(self, prompt: str) -> dict[str, Any]:
        # Serialize the START of each call so we never exceed Gemini's RPM cap.
        # The actual network round-trip happens outside the lock, so calls can
        # overlap on the wire - what matters for 429 is request *start rate*.
        async with self._lock:
            if self._calls >= self._max_calls:
                logger.warning(f"LLM call cap reached ({self._max_calls}); skipping.")
                return {}
            loop = asyncio.get_event_loop()
            elapsed = loop.time() - self._last_call_at
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                logger.debug(f"LLM rate-limit wait: {wait:.1f}s")
                await asyncio.sleep(wait)
            self._last_call_at = loop.time()
            self._calls += 1
        try:
            model = self._genai.GenerativeModel(self._model_name)
            response = await asyncio.to_thread(
                model.generate_content,
                prompt,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.1,  # deterministic
                },
            )
            txt = response.text or "{}"
            return json.loads(txt)
        except json.JSONDecodeError as e:
            logger.warning(f"LLM returned non-JSON: {e}")
            return {}
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return {}

    def _truncate(self, html: str) -> str:
        if len(html) <= self._html_truncate:
            return html
        return html[: self._html_truncate] + "\n<!-- truncated -->"

    async def classify_page(self, html_excerpt: str) -> str:
        prompt = f"""You are classifying a dental supply website page.

Return JSON: {{"page_type": "category_listing" | "product_detail" | "other"}}

- "category_listing" = page lists multiple products (cards / tiles / pagination)
- "product_detail" = single product page with name, price, SKU
- "other" = anything else (homepage, search, cart)

HTML excerpt:
{self._truncate(html_excerpt)}
"""
        result = await self._generate_json(prompt)
        return result.get("page_type", "other")

    async def extract_product_fields(
        self, html_excerpt: str, missing_fields: list[str]
    ) -> dict[str, Any]:
        fields_str = ", ".join(missing_fields)
        prompt = f"""Extract the following fields from this product detail page HTML.

Required fields: {fields_str}

Return JSON. Only include fields you find in the HTML - do NOT hallucinate.
For prices, return a number without currency symbols.
For arrays (like image_urls), return a list of strings.

HTML:
{self._truncate(html_excerpt)}
"""
        return await self._generate_json(prompt)

    async def suggest_selectors(
        self, html_excerpt: str, broken_selector: str, target_field: str
    ) -> list[str]:
        prompt = f"""A CSS selector for the field "{target_field}" stopped working.
Broken selector: {broken_selector}

Examine the HTML and propose up to 3 alternative CSS selectors that might
locate "{target_field}" on similar pages.

Return JSON: {{"selectors": ["sel1", "sel2", "sel3"]}}

HTML:
{self._truncate(html_excerpt)}
"""
        result = await self._generate_json(prompt)
        sels = result.get("selectors", [])
        return [s for s in sels if isinstance(s, str)][:3]

    async def extract_attributes(
        self, name: str, description: str, attributes: list[str]
    ) -> dict[str, str]:
        attrs_str = ", ".join(attributes)
        prompt = f"""You are extracting structured attributes from a dental supply
product. Read the product name and description and pull out the listed
attributes when there is clear evidence in the text.

Required attribute keys (only return those you find evidence for):
{attrs_str}

Rules:
- Return JSON: {{"<attr>": "<value>", ...}}
- Booleans must be "true" or "false" strings.
- Pack size example: "100/box", "50 pcs", "10 pack".
- Material examples: "nitrile", "latex", "vinyl", "polyurethane",
  "porcine gelatin", "polyester", "silk".
- intended_use should be a short noun phrase like "hemostatic" or
  "infection control".
- Omit any attribute you cannot confirm from the text.
- Do not invent values. Do not echo back the field list.

Product name: {name}
Description: {description}
"""
        result = await self._generate_json(prompt)
        if not isinstance(result, dict):
            return {}
        # Defensive coerce: only string keys, values stringified
        out: dict[str, str] = {}
        for k, v in result.items():
            if not isinstance(k, str):
                continue
            if k not in attributes:
                continue
            if v is None or v == "":
                continue
            out[k] = str(v).strip().lower() if isinstance(v, bool) else str(v).strip()
        return out


def build_llm_client(
    provider: str,
    model: str,
    max_calls: int,
    html_truncate: int,
    min_seconds_between_calls: float = 4.5,
) -> LLMClient:
    """Factory. Returns NullLLMClient if no API key - graceful degradation."""
    if provider == "gemini" and os.getenv("GEMINI_API_KEY"):
        try:
            return GeminiClient(
                model=model,
                max_calls=max_calls,
                html_truncate=html_truncate,
                min_seconds_between_calls=min_seconds_between_calls,
            )
        except Exception as e:
            logger.warning(f"Failed to init Gemini, falling back to no-op LLM: {e}")
            return NullLLMClient()
    logger.info("No LLM API key configured - running with rule-based extraction only")
    return NullLLMClient()
