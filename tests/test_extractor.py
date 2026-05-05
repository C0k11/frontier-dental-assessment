"""Smoke tests. Verify the extractor handles a synthetic Magento-style product page
without network calls.
"""
import pytest

from src.agents.extractor import ExtractorAgent
from src.config import DetailSelectors
from src.llm import NullLLMClient
from src.models import ExtractionMethod


SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Dental Glove X</title></head>
<body>
  <nav class="breadcrumbs">
    <ul>
      <li>Home</li>
      <li>Catalog</li>
      <li>Dental Exam Gloves</li>
    </ul>
  </nav>
  <main>
    <h1 class="page-title"><span>Premium Nitrile Glove, Blue, Medium</span></h1>
    <div class="product-info-main">
      <div itemprop="brand">SafeGrip</div>
      <div class="product-info-sku">SKU-12345</div>
      <div class="price-box"><span class="price">$24.99</span></div>
      <div class="product.attribute.description">
        <div class="value">Powder-free, latex-free, 100 per box.</div>
      </div>
      <link itemprop="availability" href="http://schema.org/InStock"/>
    </div>
    <div class="product media">
      <img src="https://example.com/img/glove1.jpg"/>
      <img src="https://example.com/img/glove2.jpg"/>
    </div>
    <table class="additional-attributes">
      <tr><th>Material</th><td>Nitrile</td></tr>
      <tr><th>Color</th><td>Blue</td></tr>
      <tr><th>Pack Size</th><td>100 / box</td></tr>
    </table>
  </main>
</body>
</html>
"""


def make_selectors() -> DetailSelectors:
    return DetailSelectors(
        name="h1.page-title span",
        brand="[itemprop='brand']",
        sku=".product-info-sku",
        price=".price-box .price",
        description=".product\\.attribute\\.description .value, .product-info-main .description, .value",
        pack_size=".pack-size",
        availability="[itemprop='availability']",
        images=".media img, .product.media img",
        specifications_rows=".additional-attributes tr",
        breadcrumbs=".breadcrumbs li",
    )


@pytest.mark.asyncio
async def test_extractor_basic_fields():
    extractor = ExtractorAgent(
        selectors=make_selectors(),
        llm=NullLLMClient(),
        enable_llm_fallback=False,
    )
    product = await extractor.extract(
        url="https://www.safcodental.com/catalog/glove-x.html",
        html=SAMPLE_HTML,
    )
    assert product is not None
    assert "Premium Nitrile Glove" in product.name
    assert product.brand == "SafeGrip"
    assert product.sku == "SKU-12345"
    assert product.price == 24.99
    assert product.availability == "in_stock"
    assert len(product.image_urls) == 2
    assert product.specifications.get("Material") == "Nitrile"
    assert product.extraction_method == ExtractionMethod.SELECTOR


@pytest.mark.asyncio
async def test_extractor_handles_missing_name():
    extractor = ExtractorAgent(
        selectors=make_selectors(),
        llm=NullLLMClient(),
        enable_llm_fallback=False,
    )
    product = await extractor.extract(
        url="https://www.safcodental.com/about",
        html="<html><body><p>Nothing here</p></body></html>",
    )
    # Should refuse to emit a product without a name
    assert product is None
