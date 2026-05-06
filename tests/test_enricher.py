"""Smoke tests for the EnricherAgent. No network calls.

Uses a fake LLM client with canned responses to verify the enrichment
pipeline integrates correctly with the Product schema.
"""
import pytest

from src.agents.enricher import EnricherAgent
from src.llm import LLMClient
from src.models import ExtractionMethod, Product


class FakeLLMClient(LLMClient):
    """Minimal LLM stub that returns a fixed dict and counts calls."""

    def __init__(self, response: dict):
        self.response = response
        self._calls = 0

    async def classify_page(self, html_excerpt):
        return "other"

    async def extract_product_fields(self, html_excerpt, missing_fields):
        return {}

    async def suggest_selectors(self, html_excerpt, broken_selector, target_field):
        return []

    async def extract_attributes(self, name, description, attributes):
        self._calls += 1
        return self.response

    @property
    def call_count(self):
        return self._calls


@pytest.mark.asyncio
async def test_enricher_adds_attributes_and_marks_hybrid():
    fake = FakeLLMClient(
        response={
            "material": "nitrile",
            "color": "blue",
            "powder_free": "true",
            "pack_size": "100/box",
        }
    )
    enricher = EnricherAgent(llm=fake, enabled=True)

    p = Product(
        name="Premium Nitrile Glove, Blue, Medium",
        url="https://example.com/p1",
        description="Powder-free, latex-free nitrile exam glove. Blue. 100 per box.",
        extraction_method=ExtractionMethod.SELECTOR,
    )

    enriched = await enricher.enrich(p)

    assert fake.call_count == 1
    assert enriched.specifications["material"] == "nitrile"
    assert enriched.specifications["color"] == "blue"
    assert enriched.specifications["pack_size"] == "100/box"
    assert enriched.extraction_method == ExtractionMethod.HYBRID
    assert "specifications" in enriched.fields_via_llm


@pytest.mark.asyncio
async def test_enricher_does_not_overwrite_selector_specs():
    fake = FakeLLMClient(response={"material": "nitrile", "color": "BLUE"})
    enricher = EnricherAgent(llm=fake, enabled=True)

    p = Product(
        name="X",
        url="https://example.com/x",
        description="Some text",
        specifications={"color": "Black"},  # selector already set this
        extraction_method=ExtractionMethod.SELECTOR,
    )

    enriched = await enricher.enrich(p)

    # Selector value wins
    assert enriched.specifications["color"] == "Black"
    # New keys still added
    assert enriched.specifications["material"] == "nitrile"


@pytest.mark.asyncio
async def test_enricher_disabled_short_circuits():
    fake = FakeLLMClient(response={"material": "nitrile"})
    enricher = EnricherAgent(llm=fake, enabled=False)

    p = Product(name="X", url="https://example.com/x", description="some text")
    enriched = await enricher.enrich(p)

    assert fake.call_count == 0
    assert enriched.specifications == {}
    assert enriched.extraction_method == ExtractionMethod.SELECTOR


@pytest.mark.asyncio
async def test_enricher_skips_when_no_description_and_no_name():
    fake = FakeLLMClient(response={"material": "nitrile"})
    enricher = EnricherAgent(llm=fake, enabled=True)

    # The Product schema requires `name`, so this is the closest valid case
    p = Product(name="X", url="https://example.com/x", description=None)
    p.name = ""  # bypass validator post-construction
    p.description = None
    enriched = await enricher.enrich(p)

    # With both empty, skip the LLM call
    assert fake.call_count == 0
