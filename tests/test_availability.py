from procurement_search.availability import extract_availability
from procurement_search.llm_schemas import AvailabilityGuess
from procurement_search.models import AvailabilityStatus

UNITS = {"шт": ["шт"], "упак": ["упак"], "л": ["л"], "квт": ["квт"]}


def test_extract_availability_returns_unknown_when_not_product_page(monkeypatch):
    def fake_extract(product_description, site_text, **kwargs):
        return AvailabilityGuess(
            is_product_page=False,
            status="in_stock_qty",
            quantity=100,
            quantity_unit="шт",
            pack_size_qty=None,
            pack_size_unit=None,
            min_order_qty=None,
            min_order_unit=None,
            lead_time_days=None,
            price=None,
            evidence="это карточка другого товара",
            reasoning="не то",
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_availability_with_yandexgpt", fake_extract
    )

    result = extract_availability("генератор бензиновый", "текст сайта", "https://x.example", units=UNITS)

    assert result.status == AvailabilityStatus.UNKNOWN
    assert result.quantity is None


def test_extract_availability_happy_path_maps_all_fields(monkeypatch):
    def fake_extract(product_description, site_text, **kwargs):
        assert product_description == "генератор бензиновый"
        return AvailabilityGuess(
            is_product_page=True,
            status="in_stock_qty",
            quantity=15,
            quantity_unit="шт",
            pack_size_qty=None,
            pack_size_unit=None,
            min_order_qty=2,
            min_order_unit="шт",
            lead_time_days=None,
            price="45 000 руб.",
            evidence="в наличии: 15 шт",
            reasoning="явный маркер на странице",
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_availability_with_yandexgpt", fake_extract
    )

    result = extract_availability(
        "генератор бензиновый", "текст сайта", "https://x.example/product", units=UNITS
    )

    assert result.status == AvailabilityStatus.IN_STOCK_QTY
    assert result.quantity.value == 15 and result.quantity.unit == "шт"
    assert result.min_order.value == 2
    assert result.price == "45 000 руб."
    assert result.evidence == "в наличии: 15 шт"
    assert result.source_url == "https://x.example/product"


def test_extract_availability_ignores_unit_outside_dictionary(monkeypatch):
    def fake_extract(product_description, site_text, **kwargs):
        return AvailabilityGuess(
            is_product_page=True,
            status="in_stock_qty",
            quantity=15,
            quantity_unit="дюйм",
            pack_size_qty=None,
            pack_size_unit=None,
            min_order_qty=None,
            min_order_unit=None,
            lead_time_days=None,
            price=None,
            evidence="15 дюймов",
            reasoning="ошибка модели",
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_availability_with_yandexgpt", fake_extract
    )

    result = extract_availability("товар", "текст сайта", "https://x.example", units=UNITS)

    assert result.quantity is None


def test_extract_availability_survives_llm_error(monkeypatch):
    def fake_extract(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_availability_with_yandexgpt", fake_extract
    )

    result = extract_availability("товар", "текст сайта", "https://x.example", units=UNITS)

    assert result.status == AvailabilityStatus.UNKNOWN
    assert result.evidence is None


def test_extract_availability_survives_missing_openai_package(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "procurement_search.yandexgpt_classifier":
            raise ImportError("openai не установлен")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = extract_availability("товар", "текст сайта", "https://x.example", units=UNITS)

    assert result.status == AvailabilityStatus.UNKNOWN
