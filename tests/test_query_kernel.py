"""Тесты query_kernel.py — без сети: condense_fn подменяется через
monkeypatch, тот же паттерн, что в test_brand_extractor.py."""

from procurement_search.llm_schemas import QueryKernelGuess
from procurement_search.query_kernel import KERNEL_MIN_LEN, condense_query

# Пример именно такого длинного шаблонного запроса, ради которого всё
# затевалось: юридическая вставка, цитата статьи, класс опасности, регион.
LONG_WASTE_QUERY = (
    "ищу компанию в Чувашии по услугам по обращению с отходами производства и "
    "потребления III-IV классов опасности (далее — отходы), включая сбор, "
    "транспортирование, обработку, утилизацию, обезвреживание, кроме захоронения. "
    "Обязательное наличие лицензии на осуществление деятельности по сбору, "
    "транспортированию, обработке, утилизации, обезвреживанию отходов I-IV классов "
    "опасности (в Реестре Лицензий/Разрешений - на Портале КНД) Статья 23 "
    "Федерального закона от 29.12.2014 N 458"
)


def test_disabled_by_default_no_call(monkeypatch):
    def fake_condense(*args, **kwargs):
        raise AssertionError("condense_query_with_yandexgpt не должен вызываться по умолчанию")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.condense_query_with_yandexgpt", fake_condense
    )

    assert condense_query(LONG_WASTE_QUERY) is None


def test_short_query_never_condensed_even_when_enabled(monkeypatch):
    """Короткие запросы (короче KERNEL_MIN_LEN) сжимать незачем — LLM не
    тратим даже при включённом флаге."""
    def fake_condense(*args, **kwargs):
        raise AssertionError("короткий запрос не должен уходить в LLM")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.condense_query_with_yandexgpt", fake_condense
    )

    short = "генератор бензиновый Hunter"
    assert len(short) < KERNEL_MIN_LEN
    assert condense_query(short, use_llm_fallback=True) is None


def test_condenses_long_query_when_enabled(monkeypatch):
    def fake_condense(raw_query, **kwargs):
        assert raw_query == LONG_WASTE_QUERY
        return QueryKernelGuess(
            kernel="обращение с отходами III-IV классов опасности в Чувашии",
            region="Чувашия",
            reasoning="убрал лишние цитаты и статью",
        )

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.condense_query_with_yandexgpt", fake_condense
    )

    result = condense_query(LONG_WASTE_QUERY, use_llm_fallback=True)

    assert result == "обращение с отходами III-IV классов опасности в Чувашии"


def test_returns_none_when_llm_finds_nothing_to_shorten(monkeypatch):
    def fake_condense(raw_query, **kwargs):
        return QueryKernelGuess(kernel=None, region=None, reasoning="нечего сокращать")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.condense_query_with_yandexgpt", fake_condense
    )

    result = condense_query(LONG_WASTE_QUERY, use_llm_fallback=True)

    assert result is None


def test_strips_whitespace_from_kernel(monkeypatch):
    def fake_condense(raw_query, **kwargs):
        return QueryKernelGuess(kernel="  обращение с отходами в Чувашии  ", region=None, reasoning="ok")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.condense_query_with_yandexgpt", fake_condense
    )

    result = condense_query(LONG_WASTE_QUERY, use_llm_fallback=True)

    assert result == "обращение с отходами в Чувашии"


def test_survives_api_error(monkeypatch):
    def fake_condense(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier.condense_query_with_yandexgpt", fake_condense
    )

    result = condense_query(LONG_WASTE_QUERY, use_llm_fallback=True)

    assert result is None