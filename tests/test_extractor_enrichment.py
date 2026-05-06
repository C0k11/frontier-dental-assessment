"""Smoke tests for the Extractor's LLM enrichment phase. No network calls.

Uses a fake LLM client to verify the enrichment phase integrates
correctly with the Product schema.
"""
import pytest

from src.agents.extractor import ExtractorAgent
from src.config import DetailSelectors
from src.llm import LLMClient
from src.models import ExtractionMethod, Product


class FakeLLMClient(LLMClient):
    """Minimal LLM stub returning canned responses, counting calls per method."""

    def __init__(self, attributes_response: dict = None):
        self._attrs_response = attributes_response or {}
        self._calls = 0

    async def classify_page(self, html_excerpt):
        return "other"

    async def extract_product_fields(self, html_excerpt, missing_fields):
        return {}

    async def suggest_selectors(self, html_excerpt, broken_selector, target_field):
        return []

    async def extract_attributes(self, name, description, attributes):
        self._calls += 1
        return dict(self._attrs_response)

    @property
    def call_count(self):
        return self._calls


def _selectors() -> DetailSelectors:
    return DetailSelectors(
        name="h1.page-title span",
        brand="[itemprop='brand']",
        sku="form[data-sku], .product-info-sku",
        price=".price-box .price",
        description=".value, .product-info-main .description",
        pack_size=".pack-size",
        availability="[itemprop='availability']",
        images=".media img, .product.media img",
        specifications_rows=".additional-attributes tr",
        breadcrumbs=".breadcrumbs li",
    )


SAMPLE_HTML = """
<html><body>
  <nav class="breadcrumbs"><ul>
    <li>Home</li><li>Catalog</li><li>Dental Exam Gloves</li>
  </ul></nav>
  <h1 class="page-title"><span>Premium Nitrile Glove</span></h1>
  <div itemprop="brand">SafeGrip</div>
  <form data-sku="SKU-12345"></form>
  <div class="price-box"><span class="price">$24.99</span></div>
  <div class="value">Powder-free, latex-free nitrile exam glove. Blue. 100/box.</div>
  <link itemprop="availability" href="http://schema.org/InStock"/>
  <div class="product media"><img src="https://example.com/g.jpg"/></div>
</body></html>
"""


@pytest.mark.asyncio
async def test_extractor_enrichment_promotes_to_hybrid():
    fake = FakeLLMClient(attributes_response={
        "material": "nitrile",
        "color": "blue",
        "powder_free": "true",
        "pack_size": "100/box",
    })
    extractor = ExtractorAgent(
        selectors=_selectors(),
        llm=fake,
        enable_llm_fallback=False,
        enable_enrichment=True,
    )
    product = await extractor.extract(
        url="https://example.com/p1",
        html=SAMPLE_HTML,
    )
    assert product is not None
    assert product.sku == "SKU-12345"
    assert product.specifications["material"] == "nitrile"
    assert product.specifications["color"] == "blue"
    assert product.extraction_method == ExtractionMethod.HYBRID
    assert "specifications" in product.fields_via_llm
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_extractor_enrichment_does_not_overwrite_selector_specs():
    """Selector-derived spec values should win over LLM-derived ones."""
    fake = FakeLLMClient(attributes_response={"material": "vinyl", "color": "red"})
    extractor = ExtractorAgent(
        selectors=_selectors(),
        llm=fake,
        enable_llm_fallback=False,
        enable_enrichment=True,
    )
    # Inject a spec table with material=nitrile (selector pre-fill)
    html_with_spec = SAMPLE_HTML.replace(
        '<form data-sku="SKU-12345"></form>',
        '<form data-sku="SKU-12345"></form>'
        '<table class="additional-attributes">'
        '<tr><th>material</th><td>nitrile</td></tr>'
        '</table>'
    )
    product = await extractor.extract(url="https://example.com/p2", html=html_with_spec)
    assert product is not None
    # Selector value preserved
    assert product.specifications["material"] == "nitrile"
    # LLM-only key still added
    assert product.specifications["color"] == "red"


@pytest.mark.asyncio
async def test_extractor_enrichment_disabled_skips_llm_call():
    fake = FakeLLMClient(attributes_response={"material": "nitrile"})
    extractor = ExtractorAgent(
        selectors=_selectors(),
        llm=fake,
        enable_llm_fallback=False,
        enable_enrichment=False,
    )
    product = await extractor.extract(url="https://example.com/p3", html=SAMPLE_HTML)
    assert product is not None
    assert fake.call_count == 0
    assert product.extraction_method == ExtractionMethod.SELECTOR
    assert product.specifications == {}


@pytest.mark.asyncio
async def test_extractor_enrichment_handles_empty_llm_response():
    """When LLM returns nothing extractable, product stays SELECTOR-only."""
    fake = FakeLLMClient(attributes_response={})
    extractor = ExtractorAgent(
        selectors=_selectors(),
        llm=fake,
        enable_llm_fallback=False,
        enable_enrichment=True,
    )
    product = await extractor.extract(url="https://example.com/p4", html=SAMPLE_HTML)
    assert product is not None
    assert fake.call_count == 1
    assert product.extraction_method == ExtractionMethod.SELECTOR
    assert "specifications" not in product.fields_via_llm
