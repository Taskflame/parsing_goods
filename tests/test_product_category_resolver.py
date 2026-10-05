from procurement_search.models import Candidate, IntentType, SearchIntent
from procurement_search.product_category_resolver import resolve_product_category_links


def test_resolves_exact_product_link_from_category_html(monkeypatch):
    html = """
    <html>
      <body>
        <a href="/catalog/notebooks/">All notebooks</a>
        <article>
          <a href="/product/thinkpad-p16s">Lenovo ThinkPad P16s G4</a>
          <span>mobile workstation</span>
        </article>
        <article>
          <a href="/product/thinkpad-p16v">Lenovo ThinkPad P16v Gen 2</a>
          <span>in stock, price on request</span>
        </article>
        <a href="https://other.example/product/thinkpad-p16v">External mirror</a>
      </body>
    </html>
    """
    monkeypatch.setattr(
        "procurement_search.product_category_resolver.fetch_url",
        lambda url, **kwargs: html,
    )

    category = Candidate(
        source="yandex_search",
        source_url="https://shop.example/catalog/notebooks",
        name_raw="Lenovo notebooks",
        website="https://shop.example/catalog/notebooks",
    )
    intent = SearchIntent(
        type=IntentType.PRODUCT,
        brand="Lenovo",
        model="P16v",
        entity="notebook",
    )

    resolved = resolve_product_category_links(category, intent, "Lenovo ThinkPad P16v")

    assert resolved
    assert resolved[0].website == "https://shop.example/product/thinkpad-p16v"
    assert resolved[0].source == "yandex_search:category_resolver"
    assert all("other.example" not in candidate.website for candidate in resolved)
