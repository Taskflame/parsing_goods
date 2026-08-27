"""Слой 0 (доп.): извлечение бренда из запроса байера — универсально, для
любого запроса, а не под конкретное название ("Пульсар" в design-
обсуждении — просто пример, который выявил проблему).

В отличие от attribute_extractor.py (числовые атрибуты, закрытый словарь
единиц измерения из config/units.yaml) бренд — открытый список: заранее
нельзя перечислить все существующие марки товаров, поэтому здесь нет
словарного пути, только LLM. Извлекается один раз на весь запрос (не на
кандидата) — тот же приём, что и у query_normalizer._try_llm_fallback.

Извлечённый бренд используется pipeline.py как отдельный, приоритетный
поисковый термин (бренд первым в строке) — design-обсуждение: в длинном
запросе со всеми характеристиками бренд может теряться в ранжировании
самого внешнего источника поиска (Yandex/DDG/каталог), отдельный короткий
запрос с брендом в начале даёт источнику независимый шанс найти то же
самое иначе — то есть влияет на то, что вообще НАЙДЁТСЯ, а не только на
то, как найденное отсортируется.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def extract_brand(raw_query: str, use_llm_fallback: bool = False) -> str | None:
    """None — бренд не упомянут в запросе, LLM недоступна/упала, или
    use_llm_fallback=False (по умолчанию выключено, тот же принцип, что и
    у остальных LLM-fallback шагов пайплайна — не тратим LLM без явного
    включения флага)."""
    if not use_llm_fallback:
        return None

    try:
        from procurement_search.yandexgpt_classifier import (
            extract_brand_with_yandexgpt as extract_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — извлечение бренда пропущено")
        return None

    try:
        guess = extract_fn(raw_query)
    except Exception:
        logger.warning("Извлечение бренда для запроса %r не сработало", raw_query, exc_info=True)
        return None

    return guess.brand
