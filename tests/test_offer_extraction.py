from procurement_search.attribute_extractor import Quantity
from procurement_search.models import IntentType, PageType, ProductMatch, SearchIntent
from procurement_search.offer_extraction import extract_product_offer


def test_extract_product_offer_keeps_offer_facts_with_evidence():
    intent = SearchIntent(
        type=IntentType.PRODUCT,
        entity="генератор",
        attributes={"квт": Quantity(2.5, "квт", "2,5 кВт")},
        quantity=Quantity(6.0, "шт", "6 шт"),
    )

    offer = extract_product_offer(
        intent,
        "https://shop.example/product/generator-25kw",
        PageType.PRODUCT_DETAIL,
        ProductMatch.COMPATIBLE,
        "Генератор бензиновый 2,5 кВт. Цена 50 000 руб. В наличии.",
        title="Генератор бензиновый 2,5 кВт",
    )

    assert offer.landing_url == "https://shop.example/product/generator-25kw"
    assert offer.page_type == PageType.PRODUCT_DETAIL
    assert offer.product_match == ProductMatch.COMPATIBLE
    assert offer.price == 50000.0
    assert offer.currency == "RUB"
    assert offer.attributes["квт"]["value"] == 2.5
    assert any(e.field == "price" for e in offer.evidence)


def test_extract_product_offer_does_not_copy_unconfirmed_requested_model():
    intent = SearchIntent(
        type=IntentType.PRODUCT,
        brand="Lenovo",
        model="P16v",
        identity_text="Lenovo ThinkPad P16v",
    )

    offer = extract_product_offer(
        intent,
        "https://shop.example/product/thinkpad",
        PageType.PRODUCT_DETAIL,
        ProductMatch.UNKNOWN,
        "Ноутбуки Lenovo ThinkPad в наличии. Цена 100 000 руб.",
        title="Ноутбуки Lenovo ThinkPad",
    )

    assert offer.model is None
    assert offer.brand is None
    assert not any(e.field == "model" for e in offer.evidence)
