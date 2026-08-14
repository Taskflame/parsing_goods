"""Шаг [1] пайплайна: нормализация запроса байера.

Тиражируемость (design_doc §4) держится на том, что маппинг "запрос ->
категория/синонимы/ОКВЭД" — это данные в config/categories.yaml, а не код.
Основной путь — детерминированное сопоставление по синонимам. LLM
(llm_classifier.py — Anthropic API, или ollama_classifier.py — бесплатная
локальная модель, выбор через LLM_PROVIDER) подключается только как
fallback, когда словарный матчинг не находит вообще ни одного пересечения
токенов (нечёткая формулировка байера, опечатки) — и даже тогда лишь
выбирает категорию из уже существующего списка, не придумывая коды/реестры
от себя. LLM-путь выключен по умолчанию (use_llm_fallback=False), чтобы
обычный прогон пайплайна и тесты не требовали сети и API-ключа.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from procurement_search.config import load_categories

logger = logging.getLogger(__name__)


@dataclass
class NormalizedQuery:
    raw_query: str
    category: str | None
    search_terms: list[str] = field(default_factory=list)
    okved: list[str] = field(default_factory=list)
    tnved: list[str] = field(default_factory=list)
    registries: list[str] = field(default_factory=list)


def _tokenize(text: str) -> set[str]:
    return {t.strip(".,!?()").lower() for t in text.split() if t.strip(".,!?()")}


def normalize_query(
    raw_query: str,
    categories: dict | None = None,
    use_llm_fallback: bool = False,
) -> NormalizedQuery:
    """Сопоставляет сырой запрос байера с категорией из справочника.

    Матчинг по пересечению токенов запроса с токенами синонимов категории —
    простой и прозрачный (в отличие от "спросить LLM и надеяться"), при этом
    достаточный для конечного, курируемого человеком справочника категорий.

    Если `use_llm_fallback=True` и словарный матчинг не нашёл ни одного
    пересечения — просит LLM (llm_classifier.classify_with_llm) выбрать
    ближайшую категорию из того же списка. Требует ANTHROPIC_API_KEY (или
    активный профиль `ant auth login`); при сетевой/API-ошибке откатывается
    на "категория не найдена", не роняя весь пайплайн.
    """
    categories = categories if categories is not None else load_categories()
    query_tokens = _tokenize(raw_query)

    best_category: str | None = None
    best_overlap = 0
    for category_name, spec in categories.items():
        synonym_texts = [category_name.replace("_", " ")] + list(spec.get("synonyms", []))
        for synonym in synonym_texts:
            overlap = len(query_tokens & _tokenize(synonym))
            if overlap > best_overlap:
                best_overlap = overlap
                best_category = category_name

    if best_category is None and use_llm_fallback:
        best_category = _try_llm_fallback(raw_query, list(categories.keys()))

    if best_category is None:
        # Категория не найдена (ни словарём, ни LLM-fallback'ом, если он
        # был включён) — ищем ровно по тому, что ввёл байер, без
        # обогащения ОКВЭД/реестрами.
        return NormalizedQuery(raw_query=raw_query, category=None, search_terms=[raw_query])

    spec = categories[best_category]
    search_terms = [raw_query, best_category.replace("_", " "), *spec.get("synonyms", [])]
    return NormalizedQuery(
        raw_query=raw_query,
        category=best_category,
        search_terms=list(dict.fromkeys(search_terms)),  # dedup, сохраняя порядок
        okved=list(spec.get("okved", [])),
        tnved=list(spec.get("tnved", [])),
        registries=list(spec.get("registries", [])),
    )


def _try_llm_fallback(raw_query: str, known_categories: list[str]) -> str | None:
    """Обёртка над classify_with_llm/classify_with_ollama с изоляцией сбоев.

    Провайдер выбирается переменной окружения LLM_PROVIDER:
      - "anthropic" (по умолчанию) — платный API, llm_classifier.py;
      - "ollama" — бесплатная локальная модель, ollama_classifier.py
        (требует запущенной Ollama, см. README).

    Импорт — ленивый и обёрнутый в try/except: соответствующий пакет
    ("anthropic" или ничего — Ollama использует уже имеющийся `requests`)
    нужен только тем, кто явно включает use_llm_fallback. Любая
    сетевая/API-ошибка логируется и трактуется как "категория не найдена" —
    LLM здесь вспомогательный шаг, а не критический путь (design_doc §1:
    пайплайн не должен падать целиком из-за недоступности LLM).
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    try:
        if provider == "ollama":
            from procurement_search.ollama_classifier import classify_with_ollama as classify_fn
        else:
            from procurement_search.llm_classifier import classify_with_llm as classify_fn
    except ImportError:
        logger.warning("Пакет для провайдера %r не установлен — LLM-fallback пропущен", provider)
        return None

    try:
        result = classify_fn(raw_query, known_categories)
    except Exception:
        logger.warning(
            "LLM-fallback (%s) для запроса %r не сработал", provider, raw_query, exc_info=True
        )
        return None

    if result.category is not None and result.category not in known_categories:
        # Модели явно запрещено придумывать категории вне списка (см.
        # system-промпт в llm_classifier.py) — но не доверяем этому
        # вслепую, а перепроверяем перед использованием как ключа в
        # config/categories.yaml.
        logger.warning(
            "LLM вернула категорию %r вне справочника — игнорируем", result.category
        )
        return None
    return result.category
