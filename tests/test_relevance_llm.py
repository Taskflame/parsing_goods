"""Тесты Слоя 3 (relevance_llm.py) — без сети: classify_*_with_yandexgpt
подменяются фейками через monkeypatch."""

from procurement_search.llm_schemas import (
    AttributeMatchVerdict,
    CategoryGuess,
    ContactGuess,
    LegalNameGuess,
    ListingTypeVerdict,
    PriceGuess,
    RelevanceVerdict,
    StockVerdict,
)
from procurement_search.relevance_llm import (
    check_attribute_match,
    classify_category,
    classify_listing_type,
    classify_relevance,
    classify_stock_status,
    extract_contacts,
    extract_legal_name,
    extract_price,
)


def test_classify_relevance_returns_true_from_llm(monkeypatch):
    def fake_classify(raw_query, site_text, **kwargs):
        assert raw_query == "гальванические покрытия"
        assert "цинкование" in site_text
        return RelevanceVerdict(is_relevant=True, reasoning="товар явно в каталоге")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_relevance_with_yandexgpt", fake_classify
    )

    result = classify_relevance("гальванические покрытия", "у нас есть цинкование металла")

    assert result is True


def test_classify_relevance_returns_false_from_llm(monkeypatch):
    def fake_classify(raw_query, site_text, **kwargs):
        return RelevanceVerdict(is_relevant=False, reasoning="упоминание в новости, не товар")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_relevance_with_yandexgpt", fake_classify
    )

    result = classify_relevance("гальванические покрытия", "статья про завод")

    assert result is False


def test_classify_relevance_survives_api_error(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_relevance_with_yandexgpt", fake_classify
    )

    result = classify_relevance("запрос", "текст сайта")

    assert result is None


def test_check_attribute_match_returns_false_on_numeric_mismatch(monkeypatch):
    def fake_classify(raw_query, site_text, **kwargs):
        assert raw_query == "дренажный насос 10000 л/час"
        assert "18 л/ч" in site_text
        return AttributeMatchVerdict(
            matches=False,
            mismatches=["производительность: запрошено 10000 л/час, на сайте 18 л/ч"],
            reasoning="тот же класс товара, но параметр отличается на порядки",
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attribute_match_with_yandexgpt", fake_classify
    )

    result = check_attribute_match(
        "дренажный насос 10000 л/час", "Насос дренажный Ballu Machine DC Pump, 18 л/ч"
    )

    assert result is False


def test_check_attribute_match_returns_true_when_no_contradiction(monkeypatch):
    def fake_classify(raw_query, site_text, **kwargs):
        return AttributeMatchVerdict(matches=True, mismatches=[], reasoning="характеристика не упомянута")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attribute_match_with_yandexgpt", fake_classify
    )

    result = check_attribute_match("дренажный насос 10000 л/час", "Насос дренажный в наличии")

    assert result is True


def test_check_attribute_match_survives_api_error(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attribute_match_with_yandexgpt", fake_classify
    )

    result = check_attribute_match("запрос", "текст сайта")

    assert result is None


def test_classify_listing_type_returns_false_for_article(monkeypatch):
    def fake_classify(raw_query, title, snippet, **kwargs):
        assert raw_query == "бензогенератор FinePower FPGI-1800"
        assert title == "Как работает портативный бензиновый электрогенератор"
        return ListingTypeVerdict(is_listing=False, reasoning="статья, не карточка товара")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_listing_type_with_yandexgpt", fake_classify
    )

    result = classify_listing_type(
        "бензогенератор FinePower FPGI-1800",
        "Как работает портативный бензиновый электрогенератор",
        "Разбираем принцип работы генератора",
    )

    assert result is False


def test_classify_listing_type_returns_true_for_product_page(monkeypatch):
    def fake_classify(raw_query, title, snippet, **kwargs):
        return ListingTypeVerdict(is_listing=True, reasoning="карточка товара с ценой")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_listing_type_with_yandexgpt", fake_classify
    )

    result = classify_listing_type("запрос", "Купить генератор FinePower — цена", "В наличии, доставка")

    assert result is True


def test_classify_listing_type_survives_api_error(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_listing_type_with_yandexgpt", fake_classify
    )

    result = classify_listing_type("запрос", "заголовок", "сниппет")

    assert result is None


def test_classify_stock_status_returns_true_on_explicit_badge(monkeypatch):
    def fake_classify(site_text, **kwargs):
        assert "Товар закончился" in site_text
        return StockVerdict(out_of_stock=True, reasoning="плашка 'товар закончился' на странице")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_stock_status_with_yandexgpt", fake_classify
    )

    result = classify_stock_status("Насос дренажный. Товар закончился.")

    assert result is True


def test_classify_stock_status_returns_false_without_badge(monkeypatch):
    def fake_classify(site_text, **kwargs):
        return StockVerdict(out_of_stock=False, reasoning="явного маркера нет")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_stock_status_with_yandexgpt", fake_classify
    )

    result = classify_stock_status("Насос дренажный, в каталоге компании")

    assert result is False


def test_classify_stock_status_survives_api_error(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_stock_status_with_yandexgpt", fake_classify
    )

    result = classify_stock_status("текст сайта")

    assert result is None


def test_extract_price_returns_value_from_llm(monkeypatch):
    def fake_extract(site_text, **kwargs):
        return PriceGuess(price="15 000 руб.", reasoning="указана в карточке товара")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_price_with_yandexgpt", fake_extract)

    result = extract_price("текст сайта")

    assert result == "15 000 руб."


def test_extract_price_returns_none_when_not_found(monkeypatch):
    def fake_extract(site_text, **kwargs):
        return PriceGuess(price=None, reasoning="цена по запросу, числа нет")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_price_with_yandexgpt", fake_extract)

    result = extract_price("текст сайта")

    assert result is None


def test_extract_price_survives_api_error(monkeypatch):
    def fake_extract(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_price_with_yandexgpt", fake_extract)

    result = extract_price("текст сайта")

    assert result is None


def test_classify_category_returns_code_from_llm(monkeypatch):
    def fake_classify(raw_query, categories, **kwargs):
        assert "F3" in categories
        return CategoryGuess(category="F3", reasoning="генератор — силовое электрооборудование")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.classify_category_with_yandexgpt", fake_classify)

    result = classify_category("генератор бензиновый", {"F3": {"name": "..."}})

    assert result == "F3"


def test_classify_category_normalizes_code_with_name_attached(monkeypatch):
    """Живой кейс: LLM вернула "F3: СИЛОВОЕ ЭЛЕКТРООБОРУДОВАНИЕ..." вместо
    просто "F3", несмотря на явную инструкцию в промпте — код должен
    очиститься, а не пойти дальше как невалидный ключ категории."""

    def fake_classify(raw_query, categories, **kwargs):
        return CategoryGuess(
            category="F3: СИЛОВОЕ ЭЛЕКТРООБОРУДОВАНИЕ И ДОПОЛНИТЕЛЬНОЕ ОБОРУДОВАНИЕ.",
            reasoning="генератор — силовое электрооборудование",
        )

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.classify_category_with_yandexgpt", fake_classify)

    result = classify_category("генератор бензиновый", {"F3": {"name": "..."}})

    assert result == "F3"


def test_classify_category_returns_none_for_unrecognized_code(monkeypatch):
    """Если после очистки код всё равно не находится в списке известных
    категорий — считаем категорию неопределённой, а не передаём мусорный
    код дальше по пайплайну (там он использовался бы как ключ словаря/базы)."""

    def fake_classify(raw_query, categories, **kwargs):
        return CategoryGuess(category="ЧТО-ТО СОВСЕМ НЕПОХОЖЕЕ", reasoning="...")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.classify_category_with_yandexgpt", fake_classify)

    result = classify_category("генератор бензиновый", {"F3": {"name": "..."}})

    assert result is None


def test_classify_category_returns_none_when_llm_finds_no_match(monkeypatch):
    def fake_classify(raw_query, categories, **kwargs):
        return CategoryGuess(category=None, reasoning="не подходит ни одна категория")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.classify_category_with_yandexgpt", fake_classify)

    result = classify_category("сколько сейчас времени", {"F3": {"name": "..."}})

    assert result is None


def test_classify_category_survives_api_error(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.classify_category_with_yandexgpt", fake_classify)

    result = classify_category("генератор бензиновый", {"F3": {"name": "..."}})

    assert result is None


def test_extract_legal_name_returns_value_from_llm(monkeypatch):
    def fake_extract(site_text, **kwargs):
        return LegalNameGuess(legal_name="ООО «Диптех»", reasoning="указано в футере")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_legal_name_with_yandexgpt", fake_extract
    )

    result = extract_legal_name("текст сайта")

    assert result == "ООО «Диптех»"


def test_extract_legal_name_returns_none_when_not_found(monkeypatch):
    def fake_extract(site_text, **kwargs):
        return LegalNameGuess(legal_name=None, reasoning="юрлицо не указано")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_legal_name_with_yandexgpt", fake_extract
    )

    result = extract_legal_name("текст сайта")

    assert result is None


def test_extract_legal_name_survives_api_error(monkeypatch):
    def fake_extract(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_legal_name_with_yandexgpt", fake_extract
    )

    result = extract_legal_name("текст сайта")

    assert result is None


def test_extract_contacts_returns_tuple_from_llm(monkeypatch):
    def fake_extract(site_text, **kwargs):
        return ContactGuess(phone="+7 (495) 256-16-36", email=None, address="г. Москва", reasoning="частично")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_contacts_with_yandexgpt", fake_extract
    )

    result = extract_contacts("текст сайта")

    assert result == ("+7 (495) 256-16-36", None, "г. Москва")


def test_extract_contacts_survives_api_error(monkeypatch):
    def fake_extract(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_contacts_with_yandexgpt", fake_extract
    )

    result = extract_contacts("текст сайта")

    assert result is None
