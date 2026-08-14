"""Слой 3 уточнения релевантности — точечная LLM-проверка (design-
обсуждение скоринга, см. scoring.py про Слой 1 и site_relevance.py про
Слой 2). Вопрос вида "есть ли на этом сайте <товар> как реальная позиция
номенклатуры, а не случайное упоминание" — Слой 2 (пересечение токенов по
тексту сайта) этого не отличает, для этого и нужна LLM.

Применяется ТОЛЬКО к top-N кандидатам, уже прошедшим Слои 1-2 (см.
pipeline.py) — вызывать LLM на каждого из 100-200 кандидатов было бы
медленно и (для платного провайдера) дорого.

Провайдер и паттерн выбора — те же, что в query_normalizer._try_llm_fallback
(LLM_PROVIDER=anthropic|ollama, ленивый импорт, любая ошибка -> None, а не
падение пайплайна) — переиспользуем инфраструктуру, а не заводим новую.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def classify_relevance(raw_query: str, site_text: str) -> bool | None:
    """None — LLM недоступна (сеть/ключ/пакет не установлен) или упала —
    вызывающий код должен трактовать это как "нет сигнала", а не как
    "нерелевантно" (см. pipeline.py: relevance не трогается, если None)."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    try:
        if provider == "ollama":
            from procurement_search.ollama_classifier import (
                classify_relevance_with_ollama as classify_fn,
            )
        else:
            from procurement_search.llm_classifier import (
                classify_relevance_with_llm as classify_fn,
            )
    except ImportError:
        logger.warning(
            "Пакет для провайдера %r не установлен — LLM-проверка релевантности (Слой 3) пропущена",
            provider,
        )
        return None

    try:
        result = classify_fn(raw_query, site_text)
    except Exception:
        logger.warning(
            "LLM-проверка релевантности (%s) для запроса %r не сработала",
            provider,
            raw_query,
            exc_info=True,
        )
        return None
    return result.is_relevant
