"""Общие Pydantic-схемы structured output для всех LLM-провайдеров
(yandexgpt_classifier.py — боевой по умолчанию, cloudru_classifier.py —
запасной вариант). Вынесены в отдельный модуль, а не определены в одном
из провайдеров, чтобы ни один провайдер не был "главным" источником
контракта для остальных — оба импортируют схемы отсюда на равных.
"""

from __future__ import annotations

from pydantic import BaseModel


class RelevanceVerdict(BaseModel):
    is_relevant: bool
    reasoning: str


class BrandGuess(BaseModel):
    brand: str | None
    reasoning: str


class AttributeBatchItem(BaseModel):
    value: str
    unit: str | None
    raw: str
    reasoning: str


class AttributeBatchGuess(BaseModel):
    attributes: list[AttributeBatchItem]


class AttributeMatchVerdict(BaseModel):
    matches: bool
    mismatches: list[str]
    reasoning: str


class ListingTypeVerdict(BaseModel):
    is_listing: bool
    reasoning: str


class StockVerdict(BaseModel):
    out_of_stock: bool
    reasoning: str


class ContactGuess(BaseModel):
    phone: str | None
    email: str | None
    address: str | None
    reasoning: str
