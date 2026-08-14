"""Тесты Слоя 3 (relevance_llm.py) — без сети, тот же паттерн, что
test_llm_fallback.py: classify_relevance_with_llm/_ollama подменяются."""

from procurement_search.llm_classifier import RelevanceVerdict
from procurement_search.relevance_llm import classify_relevance


def test_classify_relevance_returns_true_from_llm(monkeypatch):
    def fake_classify(raw_query, site_text, **kwargs):
        assert raw_query == "гальванические покрытия"
        assert "цинкование" in site_text
        return RelevanceVerdict(is_relevant=True, reasoning="товар явно в каталоге")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_relevance_with_llm", fake_classify
    )

    result = classify_relevance("гальванические покрытия", "у нас есть цинкование металла")

    assert result is True


def test_classify_relevance_returns_false_from_llm(monkeypatch):
    def fake_classify(raw_query, site_text, **kwargs):
        return RelevanceVerdict(is_relevant=False, reasoning="упоминание в новости, не товар")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_relevance_with_llm", fake_classify
    )

    result = classify_relevance("гальванические покрытия", "статья про завод")

    assert result is False


def test_classify_relevance_survives_api_error(monkeypatch):
    def fake_classify(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.llm_classifier.classify_relevance_with_llm", fake_classify
    )

    result = classify_relevance("запрос", "текст сайта")

    assert result is None


def test_classify_relevance_uses_ollama_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    def fake_anthropic(*args, **kwargs):
        raise AssertionError("должен вызываться ollama, а не anthropic-провайдер")

    def fake_ollama(raw_query, site_text, **kwargs):
        return RelevanceVerdict(is_relevant=True, reasoning="локальная модель")

    monkeypatch.setattr("procurement_search.llm_classifier.classify_relevance_with_llm", fake_anthropic)
    monkeypatch.setattr("procurement_search.ollama_classifier.classify_relevance_with_ollama", fake_ollama)

    result = classify_relevance("запрос", "текст сайта")

    assert result is True


def test_classify_relevance_survives_ollama_connection_error(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    def fake_ollama(*args, **kwargs):
        raise ConnectionError("Ollama не запущена")

    monkeypatch.setattr("procurement_search.ollama_classifier.classify_relevance_with_ollama", fake_ollama)

    result = classify_relevance("запрос", "текст сайта")

    assert result is None
