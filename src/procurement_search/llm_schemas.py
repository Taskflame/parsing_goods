"""Общие Pydantic-схемы structured output для LLM-провайдера
(yandexgpt_classifier.py). Вынесены в отдельный модуль, а не определены
прямо в yandexgpt_classifier.py — чтобы схемы контракта были отделены от
кода, который их использует.
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


class PriceGuess(BaseModel):
    price: str | None
    reasoning: str


class LegalNameGuess(BaseModel):
    legal_name: str | None
    reasoning: str


class CategoryGuess(BaseModel):
    category: str | None
    reasoning: str


class StockVerdict(BaseModel):
    out_of_stock: bool
    reasoning: str


class ContactGuess(BaseModel):
    phone: str | None
    email: str | None
    address: str | None
    reasoning: str
