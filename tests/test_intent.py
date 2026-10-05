from procurement_search.attribute_extractor import classify_roles, extract_attributes
from procurement_search.config import load_spec_ranges, load_units
from procurement_search.intent import classify_page_type, match_product, match_service, parse_intent
from procurement_search.models import IntentType, PageType, ProductMatch, ServiceMatch


def _intent(raw_query: str, brand: str | None = None):
    units = load_units()
    extraction = extract_attributes(raw_query, units=units)
    parsed = classify_roles(extraction, raw_query, brand, units, spec_ranges=load_spec_ranges())
    return parse_intent(raw_query, parsed_query=parsed, extraction=extraction, brand=brand)


def test_product_intent_keeps_order_quantity_out_of_identity():
    intent = _intent("Lenovo ThinkPad P16v 100 шт", brand="Lenovo")

    assert intent.type == IntentType.PRODUCT
    assert intent.brand == "Lenovo"
    assert intent.model == "P16v"
    assert intent.quantity is not None
    assert intent.quantity.value == 100


def test_product_match_rejects_nearby_thinkpad_models():
    intent = _intent("Lenovo ThinkPad P16v 100 шт", brand="Lenovo")

    assert match_product(intent, "Lenovo ThinkPad P16v Gen 2") == ProductMatch.EXACT
    assert match_product(intent, "Lenovo ThinkPad P16s G4") == ProductMatch.MISMATCH
    assert match_product(intent, "Lenovo ThinkPad P14s Gen 6") == ProductMatch.MISMATCH
    assert match_product(intent, "Lenovo ThinkPad P1 Gen 5") == ProductMatch.MISMATCH


def test_product_match_without_model_checks_attributes_as_compatible_not_exact():
    intent = _intent("генератор бензиновый 2,5 кВт 6 шт")

    assert intent.type == IntentType.PRODUCT
    assert intent.quantity is not None
    assert intent.quantity.value == 6
    assert match_product(intent, "Генератор бензиновый 2,5 кВт") == ProductMatch.COMPATIBLE
    assert match_product(intent, "Генератор бензиновый 2500 Вт") == ProductMatch.COMPATIBLE
    assert match_product(intent, "Генератор бензиновый S2300IS 1,8 кВт") == ProductMatch.MISMATCH
    assert match_product(intent, "Генератор бензиновый в наличии") == ProductMatch.UNKNOWN


def test_service_intent_accepts_service_pages():
    intent = _intent("утилизация и обезвреживание отходов")

    assert intent.type == IntentType.SERVICE
    assert classify_page_type("https://eco.example/uslugi/utilizaciya/", "Утилизация отходов") == PageType.SERVICE_DETAIL
    assert match_service(intent, "Утилизация и обезвреживание отходов", "Оказываем услуги") == ServiceMatch.MATCH


def test_directory_is_not_final_offer_page_type():
    assert (
        classify_page_type(
            "https://2gis.ru/moscow/search/utilizatsiya-othodov",
            "Утилизация отходов: компании",
            "Справочник организаций",
        )
        == PageType.DIRECTORY
    )
