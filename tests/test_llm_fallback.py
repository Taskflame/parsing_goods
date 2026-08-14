"""Тесты LLM-fallback в query_normalizer.py — без сети и без API-ключа.

classify_with_llm подменяется через monkeypatch: сеть/API реального
Anthropic-клиента здесь не нужна и не должна требоваться для тестов.
"""

from procurement_search.llm_classifier import CategoryMatch
from procurement_search.query_normalizer import normalize_query

CATEGORIES = {
    "Гальванические_покрытия": {
        "synonyms": ["анодирование", "цинкование"],
        "okved": ["25.61"],
        "tnved": [],
        "registries": [],
    },
}


def test_llm_fallback_disabled_by_default_no_call(monkeypatch):
    called = {"n": 0}

    def fake_classify(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("classify_with_llm не должен вызываться по умолчанию")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_with_llm", fake_classify
    )

    result = normalize_query("покрыть металл цинком", categories=CATEGORIES)

    assert called["n"] == 0
    assert result.category is None


def test_llm_fallback_used_when_enabled_and_dict_match_fails(monkeypatch):
    def fake_classify(raw_query, known_categories, **kwargs):
        assert raw_query == "покрыть металл цинком"
        assert "Гальванические_покрытия" in known_categories
        return CategoryMatch(category="Гальванические_покрытия", reasoning="семантически близко")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_with_llm", fake_classify
    )

    result = normalize_query(
        "покрыть металл цинком", categories=CATEGORIES, use_llm_fallback=True
    )

    assert result.category == "Гальванические_покрытия"
    assert result.okved == ["25.61"]


def test_llm_fallback_ignores_category_outside_dictionary(monkeypatch):
    def fake_classify(raw_query, known_categories, **kwargs):
        return CategoryMatch(category="Несуществующая_категория", reasoning="ошибка модели")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_with_llm", fake_classify
    )

    result = normalize_query(
        "совсем другой запрос", categories=CATEGORIES, use_llm_fallback=True
    )

    assert result.category is None


def test_llm_fallback_survives_api_error(monkeypatch):
    def fake_classify(raw_query, known_categories, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_with_llm", fake_classify
    )

    result = normalize_query(
        "совсем другой запрос", categories=CATEGORIES, use_llm_fallback=True
    )

    assert result.category is None
    assert result.search_terms == ["совсем другой запрос"]


def test_dictionary_match_takes_priority_over_llm(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise AssertionError("LLM не должен вызываться, если словарь уже нашёл совпадение")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_with_llm", fake_classify
    )

    result = normalize_query(
        "гальванические покрытия", categories=CATEGORIES, use_llm_fallback=True
    )

    assert result.category == "Гальванические_покрытия"


def test_llm_provider_ollama_used_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    def fake_classify_anthropic(*args, **kwargs):
        raise AssertionError("должен вызываться ollama, а не anthropic-провайдер")

    def fake_classify_ollama(raw_query, known_categories, **kwargs):
        return CategoryMatch(category="Гальванические_покрытия", reasoning="локальная модель")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_with_llm", fake_classify_anthropic
    )
    monkeypatch.setattr(
        "procurement_search.ollama_classifier.classify_with_ollama", fake_classify_ollama
    )

    result = normalize_query(
        "покрыть металл цинком", categories=CATEGORIES, use_llm_fallback=True
    )

    assert result.category == "Гальванические_покрытия"


def test_llm_provider_ollama_survives_connection_error(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    def fake_classify_ollama(*args, **kwargs):
        raise ConnectionError("Ollama не запущена")

    monkeypatch.setattr(
        "procurement_search.ollama_classifier.classify_with_ollama", fake_classify_ollama
    )

    result = normalize_query(
        "покрыть металл цинком", categories=CATEGORIES, use_llm_fallback=True
    )

    assert result.category is None
