"""Тесты brand_extractor.py — без сети: classify_fn подменяется через
monkeypatch, тот же паттерн, что в test_llm_fallback.py."""

from procurement_search.brand_extractor import extract_brand
from procurement_search.llm_schemas import BrandGuess


def test_disabled_by_default_no_call(monkeypatch):
    def fake_extract(*args, **kwargs):
        raise AssertionError("extract_brand_with_yandexgpt не должен вызываться по умолчанию")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_brand_with_yandexgpt", fake_extract)

    result = extract_brand("частотный преобразователь пульсар 380 вольт")

    assert result is None


def test_extracts_brand_when_enabled(monkeypatch):
    def fake_extract(raw_query, **kwargs):
        assert raw_query == "частотный преобразователь пульсар 380 вольт"
        return BrandGuess(brand="Пульсар", reasoning="явно указан бренд")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_brand_with_yandexgpt", fake_extract)

    result = extract_brand("частотный преобразователь пульсар 380 вольт", use_llm_fallback=True)

    assert result == "Пульсар"


def test_returns_none_when_no_brand_in_query(monkeypatch):
    def fake_extract(raw_query, **kwargs):
        return BrandGuess(brand=None, reasoning="бренд не упомянут")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_brand_with_yandexgpt", fake_extract)

    result = extract_brand("насос дренажный 10000 л/час", use_llm_fallback=True)

    assert result is None


def test_survives_api_error(monkeypatch):
    def fake_extract(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_brand_with_yandexgpt", fake_extract)

    result = extract_brand("частотный преобразователь пульсар", use_llm_fallback=True)

    assert result is None
