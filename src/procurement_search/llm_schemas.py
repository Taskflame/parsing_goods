"""Общие Pydantic-схемы structured output для LLM-провайдера
(yandexgpt_classifier.py). Вынесены в отдельный модуль, а не определены
прямо в yandexgpt_classifier.py — чтобы схемы контракта были отделены от
кода, который их использует.
"""

from __future__ import annotations

from typing import Literal

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


class QueryKernelGuess(BaseModel):
    """Слой 0 (см. query_kernel.py): компактное поисковое ядро длинного,
    шаблонного запроса байера (юридические вставки, ссылки на законы,
    цитаты статей, требования к лицензии и т.п. сжимаются до того, что
    реально ищет байер) — чтобы короткий осмысленный запрос уходил в
    поисковик, а не весь сырой текст, топящий важные слова в мусоре.

    kernel — короткая естественная фраза, которую можно отдать поисковику:
    ЧТО ищет (товар/услуга/компания) + ГДЕ (регион, если указан) + ключевые
    квалификаторы (класс опасности, отрасль и т.п.). Без ссылок на
    нормативные акты, номеров статей и канцелярита "включая/кроме/далее".
    region — регион, если байер его упомянул ("в Чувашии"), иначе null.

    ВАЖНО: все поля объявлены без дефолта — Yandex в strict-режиме
    json_schema требует, чтобы каждое поле было обязательным в объекте
    (допустимое значение null у `str | None` при этом не нарушает — null
    в обязательное поле модель вернуть может). Fallback-дефолт `= None`
    здесь дал бы отказ API: "all fields must be required".
    """
    kernel: str | None
    region: str | None
    reasoning: str


class StockVerdict(BaseModel):
    status: Literal["in_stock", "clarify", "out_of_stock", "unknown"]
    quote: str | None
    reasoning: str


class ContactGuess(BaseModel):
    phone: str | None
    email: str | None
    address: str | None
    reasoning: str


class AvailabilityGuess(BaseModel):
    """См. availability.extract_availability. Плоские поля (не вложенные
    объекты, как в исходной постановке задачи с "фасовка": {...}) — тот же
    принцип, что и у остальных схем в этом файле: json_schema strict-режим
    работает надёжнее с плоской структурой, вложенность не даёт здесь
    ничего сверх нескольких дополнительных полей."""

    is_product_page: bool
    status: Literal["in_stock_qty", "in_stock", "on_order", "out_of_stock", "unknown"]
    quantity: float | None
    quantity_unit: str | None
    pack_size_qty: float | None
    pack_size_unit: str | None
    min_order_qty: float | None
    min_order_unit: str | None
    lead_time_days: int | None
    price: str | None
    evidence: str | None
    reasoning: str


class ElementLocatorGuess(BaseModel):
    """См. stepper_probe.py, третий (самый дорогой) шаг эвристики поиска
    степпера — когда детерминированные селекторы и клик по типовой кнопке
    "В корзину" не сработали. element_index — индекс в пронумерованном
    списке кликабельных элементов страницы (см. промпт), который сама
    модель посчитала кнопкой "+"/"добавить в корзину"; null, если ни один
    элемент явно не подошёл — не угадываем наугад, честное "не нашли"."""

    element_index: int | None
    reasoning: str
