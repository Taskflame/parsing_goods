"""Шаг [1] пайплайна: нормализация запроса байера.

Раньше здесь было сопоставление запроса со справочником категорий
(config/categories.yaml, ~4 тестовых категории) по пересечению токенов, с
LLM-fallback на случай, если словарь не находил совпадения — design_doc
§4 описывал это как основу "тиражируемости". На практике для разнородных
реальных запросов справочник почти всегда не находил совпадения (слишком
мало категорий), а `okved`/`tnved`/`registries`, которые от найденной
категории зависели, нигде не читались дальше по пайплайну — убрано целиком
как мёртвый вес, а не доработано (design-обсуждение).

`NormalizedQuery` осталась тем же контрактом, что используют scoring.py
(query_tokens/compute_score -> weights_for_category) и pipeline.py — сейчас
это тонкая обёртка над raw_query, `category` всегда `None`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NormalizedQuery:
    raw_query: str
    category: str | None = None
    search_terms: list[str] = field(default_factory=list)


def normalize_query(raw_query: str) -> NormalizedQuery:
    return NormalizedQuery(raw_query=raw_query, search_terms=[raw_query])
