"""Тесты Слоя 3 (relevance_llm.py) — без сети, тот же паттерн, что
test_llm_fallback.py: classify_*_with_yandexgpt/_cloudru подменяются.

Тесты без explicit LLM_PROVIDER мокают yandexgpt_classifier — это
провайдер по умолчанию (см. relevance_llm.py / query_normalizer.
_try_llm_fallback)."""

from procurement_search.llm_schemas import (
    AttributeMatchVerdict,
    ContactGuess,
    ListingTypeVerdict,
    RelevanceVerdict,
    StockVerdict,
)
from procurement_search.relevance_llm import (
    check_attribute_match,
    classify_listing_type,
    classify_relevance,
    classify_stock_status,
    extract_contacts,
)


def test_classify_relevance_returns_true_from_llm(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

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
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(raw_query, site_text, **kwargs):
        return RelevanceVerdict(is_relevant=False, reasoning="упоминание в новости, не товар")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_relevance_with_yandexgpt", fake_classify
    )

    result = classify_relevance("гальванические покрытия", "статья про завод")

    assert result is False


def test_classify_relevance_survives_api_error(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_relevance_with_yandexgpt", fake_classify
    )

    result = classify_relevance("запрос", "текст сайта")

    assert result is None


def test_classify_relevance_uses_cloudru_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cloudru")

    def fake_yandexgpt(*args, **kwargs):
        raise AssertionError("должен вызываться cloudru, а не yandexgpt (дефолт)")

    def fake_cloudru(raw_query, site_text, **kwargs):
        return RelevanceVerdict(is_relevant=True, reasoning="Cloud.ru")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_relevance_with_yandexgpt", fake_yandexgpt
    )
    monkeypatch.setattr("procurement_search.cloudru_classifier.classify_relevance_with_cloudru", fake_cloudru)

    result = classify_relevance("запрос", "текст сайта")

    assert result is True


def test_check_attribute_match_returns_false_on_numeric_mismatch(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

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
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(raw_query, site_text, **kwargs):
        return AttributeMatchVerdict(matches=True, mismatches=[], reasoning="характеристика не упомянута")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attribute_match_with_yandexgpt", fake_classify
    )

    result = check_attribute_match("дренажный насос 10000 л/час", "Насос дренажный в наличии")

    assert result is True


def test_check_attribute_match_survives_api_error(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attribute_match_with_yandexgpt", fake_classify
    )

    result = check_attribute_match("запрос", "текст сайта")

    assert result is None


def test_check_attribute_match_uses_cloudru_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cloudru")

    def fake_yandexgpt(*args, **kwargs):
        raise AssertionError("должен вызываться cloudru, а не yandexgpt (дефолт)")

    def fake_cloudru(raw_query, site_text, **kwargs):
        return AttributeMatchVerdict(matches=False, mismatches=["мощность"], reasoning="Cloud.ru")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_attribute_match_with_yandexgpt", fake_yandexgpt
    )
    monkeypatch.setattr(
        "procurement_search.cloudru_classifier.classify_attribute_match_with_cloudru", fake_cloudru
    )

    result = check_attribute_match("запрос", "текст сайта")

    assert result is False


def test_classify_listing_type_returns_false_for_article(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

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
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(raw_query, title, snippet, **kwargs):
        return ListingTypeVerdict(is_listing=True, reasoning="карточка товара с ценой")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_listing_type_with_yandexgpt", fake_classify
    )

    result = classify_listing_type("запрос", "Купить генератор FinePower — цена", "В наличии, доставка")

    assert result is True


def test_classify_listing_type_survives_api_error(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_listing_type_with_yandexgpt", fake_classify
    )

    result = classify_listing_type("запрос", "заголовок", "сниппет")

    assert result is None


def test_classify_listing_type_uses_cloudru_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cloudru")

    def fake_yandexgpt(*args, **kwargs):
        raise AssertionError("должен вызываться cloudru, а не yandexgpt (дефолт)")

    def fake_cloudru(raw_query, title, snippet, **kwargs):
        return ListingTypeVerdict(is_listing=False, reasoning="Cloud.ru")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_listing_type_with_yandexgpt", fake_yandexgpt
    )
    monkeypatch.setattr(
        "procurement_search.cloudru_classifier.classify_listing_type_with_cloudru", fake_cloudru
    )

    result = classify_listing_type("запрос", "заголовок", "сниппет")

    assert result is False


def test_classify_stock_status_returns_true_on_explicit_badge(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(site_text, **kwargs):
        assert "Товар закончился" in site_text
        return StockVerdict(out_of_stock=True, reasoning="плашка 'товар закончился' на странице")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_stock_status_with_yandexgpt", fake_classify
    )

    result = classify_stock_status("Насос дренажный. Товар закончился.")

    assert result is True


def test_classify_stock_status_returns_false_without_badge(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(site_text, **kwargs):
        return StockVerdict(out_of_stock=False, reasoning="явного маркера нет")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_stock_status_with_yandexgpt", fake_classify
    )

    result = classify_stock_status("Насос дренажный, в каталоге компании")

    assert result is False


def test_classify_stock_status_survives_api_error(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_stock_status_with_yandexgpt", fake_classify
    )

    result = classify_stock_status("текст сайта")

    assert result is None


def test_classify_stock_status_uses_cloudru_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cloudru")

    def fake_yandexgpt(*args, **kwargs):
        raise AssertionError("должен вызываться cloudru, а не yandexgpt (дефолт)")

    def fake_cloudru(site_text, **kwargs):
        return StockVerdict(out_of_stock=True, reasoning="Cloud.ru")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.classify_stock_status_with_yandexgpt", fake_yandexgpt
    )
    monkeypatch.setattr("procurement_search.cloudru_classifier.classify_stock_status_with_cloudru", fake_cloudru)

    result = classify_stock_status("текст сайта")

    assert result is True


def test_extract_contacts_returns_tuple_from_llm(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_extract(site_text, **kwargs):
        return ContactGuess(phone="+7 (495) 256-16-36", email=None, address="г. Москва", reasoning="частично")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_contacts_with_yandexgpt", fake_extract
    )

    result = extract_contacts("текст сайта")

    assert result == ("+7 (495) 256-16-36", None, "г. Москва")


def test_extract_contacts_survives_api_error(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_extract(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_contacts_with_yandexgpt", fake_extract
    )

    result = extract_contacts("текст сайта")

    assert result is None


def test_extract_contacts_uses_cloudru_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cloudru")

    def fake_yandexgpt(*args, **kwargs):
        raise AssertionError("должен вызываться cloudru, а не yandexgpt (дефолт)")

    def fake_cloudru(site_text, **kwargs):
        return ContactGuess(phone=None, email="info@company.ru", address=None, reasoning="Cloud.ru")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.extract_contacts_with_yandexgpt", fake_yandexgpt
    )
    monkeypatch.setattr("procurement_search.cloudru_classifier.extract_contacts_with_cloudru", fake_cloudru)

    result = extract_contacts("текст сайта")

    assert result == (None, "info@company.ru", None)
