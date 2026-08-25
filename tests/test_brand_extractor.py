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
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_extract(raw_query, **kwargs):
        assert raw_query == "частотный преобразователь пульсар 380 вольт"
        return BrandGuess(brand="Пульсар", reasoning="явно указан бренд")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_brand_with_yandexgpt", fake_extract)

    result = extract_brand("частотный преобразователь пульсар 380 вольт", use_llm_fallback=True)

    assert result == "Пульсар"


def test_returns_none_when_no_brand_in_query(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_extract(raw_query, **kwargs):
        return BrandGuess(brand=None, reasoning="бренд не упомянут")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_brand_with_yandexgpt", fake_extract)

    result = extract_brand("насос дренажный 10000 л/час", use_llm_fallback=True)

    assert result is None


def test_survives_api_error(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    def fake_extract(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_brand_with_yandexgpt", fake_extract)

    result = extract_brand("частотный преобразователь пульсар", use_llm_fallback=True)

    assert result is None


def test_uses_cloudru_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cloudru")

    def fake_yandexgpt(*args, **kwargs):
        raise AssertionError("должен вызываться cloudru, а не yandexgpt (дефолт)")

    def fake_cloudru(raw_query, **kwargs):
        return BrandGuess(brand="Пульсар", reasoning="Cloud.ru")

    monkeypatch.setattr("procurement_search.yandexgpt_classifier.extract_brand_with_yandexgpt", fake_yandexgpt)
    monkeypatch.setattr("procurement_search.cloudru_classifier.extract_brand_with_cloudru", fake_cloudru)

    result = extract_brand("частотный преобразователь пульсар", use_llm_fallback=True)

    assert result == "Пульсар"
