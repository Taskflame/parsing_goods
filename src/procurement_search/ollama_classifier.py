"""Бесплатная альтернатива llm_classifier.py — та же задача (fallback-
классификация категории, см. llm_classifier.py про постановку задачи),
но через локально запущенную Ollama вместо платного Anthropic API.

Требует установленной и запущенной Ollama (https://ollama.com) со
скачанной моделью, например:

    ollama pull llama3.2
    ollama serve   # обычно уже запущен как сервис после установки

Никаких новых зависимостей — Ollama отдаёт обычный HTTP REST API на
localhost, используется тот же `requests`, что и в sources/base.py.
Переиспользует CategoryMatch из llm_classifier.py, чтобы у обоих
провайдеров был идентичный контракт и query_normalizer.py мог
подключить любой без изменений в остальном коде.
"""

from __future__ import annotations

import os

import requests

from procurement_search.llm_classifier import AttributeGuess, CategoryMatch, RelevanceVerdict

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")


def classify_with_ollama(
    raw_query: str,
    known_categories: list[str],
    base_url: str = OLLAMA_URL,
    model: str = OLLAMA_MODEL,
    timeout: float = 30.0,
) -> CategoryMatch:
    """Тот же контракт, что у llm_classifier.classify_with_llm.

    Ollama (с версии 0.5) поддерживает structured output через параметр
    `format` с JSON-схемой — используем схему CategoryMatch напрямую,
    без ручного парсинга текста.
    """
    prompt = (
        "Ты помогаешь классифицировать запрос байера по категориям закупки. "
        "Выбери категорию СТРОГО из списка ниже, без выдумывания новых. "
        "Если ни одна категория семантически не подходит — верни category: null.\n\n"
        f"Запрос байера: {raw_query!r}\n"
        f"Известные категории: {known_categories}"
    )

    response = requests.post(
        base_url,
        json={
            "model": model,
            "prompt": prompt,
            "format": CategoryMatch.model_json_schema(),
            "stream": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    raw_json = response.json()["response"]
    return CategoryMatch.model_validate_json(raw_json)


def classify_relevance_with_ollama(
    raw_query: str,
    site_text: str,
    base_url: str = OLLAMA_URL,
    model: str = OLLAMA_MODEL,
    timeout: float = 30.0,
) -> RelevanceVerdict:
    """Тот же контракт, что у llm_classifier.classify_relevance_with_llm —
    Слой 3 уточнения релевантности, бесплатный локальный вариант."""
    prompt = (
        "Ты проверяешь, продаёт ли компания на своём сайте конкретный товар/услугу, "
        "указанный байером — именно как позицию в номенклатуре, а не случайное "
        "упоминание в новости или статье.\n\n"
        f"Запрос байера: {raw_query!r}\n\n"
        f"Текст сайта компании (может быть обрезан): {site_text[:8000]!r}"
    )

    response = requests.post(
        base_url,
        json={
            "model": model,
            "prompt": prompt,
            "format": RelevanceVerdict.model_json_schema(),
            "stream": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    raw_json = response.json()["response"]
    return RelevanceVerdict.model_validate_json(raw_json)


def classify_attribute_with_ollama(
    raw_query: str,
    number: str,
    known_units: list[str],
    base_url: str = OLLAMA_URL,
    model: str = OLLAMA_MODEL,
    timeout: float = 30.0,
) -> AttributeGuess:
    """Тот же контракт, что у llm_classifier.classify_attribute_with_llm."""
    prompt = (
        "Ты помогаешь понять, является ли число в запросе байера технической "
        "характеристикой товара (например, мощность, размер, масса), и если да — "
        "к какой единице измерения оно относится. Выбери единицу СТРОГО из "
        "переданного списка кодов, без выдумывания новых. Если число — это "
        "количество штук без явной единицы, часть артикула/названия, год или "
        "что-то ещё, не являющееся характеристикой с единицей измерения из "
        "списка — верни unit: null.\n\n"
        f"Запрос байера: {raw_query!r}\n"
        f"Число, для которого нужно определить единицу: {number!r}\n"
        f"Известные единицы измерения (коды): {known_units}"
    )

    response = requests.post(
        base_url,
        json={
            "model": model,
            "prompt": prompt,
            "format": AttributeGuess.model_json_schema(),
            "stream": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    raw_json = response.json()["response"]
    return AttributeGuess.model_validate_json(raw_json)