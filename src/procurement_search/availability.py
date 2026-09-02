"""Слой 4 пайплайна: наличие товара с числом на сайте кандидата (см.
models.Availability, quantity_match.py). Единственный источник данных —
один LLM-вызов над уже скачанным текстом сайта (relevance_llm.extract_availability),
без лестницы JSON-LD/CMS-шаблонов/regex из исходной постановки задачи — по
прямой просьбе пользователя, тот же паттерн, что уже применён для
relevance_llm.classify_stock_status/extract_price/extract_contacts в этом
проекте (design-обсуждение: рунет слишком разнороден по вёрстке, чтобы
покрыть его конечным набором шаблонов, а один явный LLM-вызов дешевле в
поддержке, чем детектор CMS + три набора regex/CSS-селекторов).

Текст страницы переиспользуется, а не скачивается заново: `site_relevance.crawl_site_text`
уже приоритизирует URL, который реально нашёлся в поиске (см.
sources/google_cse.py:website=link, sources/yandex_search.py:website=url) —
в большинстве случаев это уже карточка конкретного товара, а не главная
страница сайта. Отдельного шага "поиск карточки товара" (internal search/
sitemap.xml, как в исходной постановке задачи) поэтому не строим — см.
docs/design_doc.md про эту находку.

Вызывается pipeline._refine_relevance только для top-N кандидатов после
скоринга, только при check_availability=True — тот же паттерн, что у
Слоя 3 (deep_relevance_top_n), лишний LLM-вызов на кандидата не бесплатен.
"""

from __future__ import annotations

import logging
from datetime import datetime

from procurement_search import relevance_llm
from procurement_search.attribute_extractor import Quantity
from procurement_search.config import load_units
from procurement_search.models import Availability, AvailabilityStatus

logger = logging.getLogger(__name__)

_STATUS_MAP: dict[str, AvailabilityStatus] = {
    "in_stock_qty": AvailabilityStatus.IN_STOCK_QTY,
    "in_stock": AvailabilityStatus.IN_STOCK,
    "on_order": AvailabilityStatus.ON_ORDER,
    "out_of_stock": AvailabilityStatus.OUT_OF_STOCK,
    "unknown": AvailabilityStatus.UNKNOWN,
}


def _unknown(source_url: str) -> Availability:
    return Availability(
        status=AvailabilityStatus.UNKNOWN,
        quantity=None,
        pack_size=None,
        min_order=None,
        lead_time_days=None,
        price=None,
        source_url=source_url,
        checked_at=datetime.now(),
        evidence=None,
    )


def _quantity_from_guess(value: float | None, unit: str | None, units: dict) -> Quantity | None:
    """Тот же принцип, что и в attribute_extractor._try_llm_fallback: не
    доверяем единице от LLM вслепую, она обязана быть из уже существующего
    словаря units.yaml, иначе — None, а не выдуманная величина."""
    if value is None or unit is None:
        return None
    canonical = unit.strip().lower()
    if canonical not in units:
        logger.warning(
            "LLM-извлечение наличия вернуло единицу %r вне словаря units.yaml — игнорируем", unit
        )
        return None
    return Quantity(value=value, unit=canonical, raw=f"{value:g} {canonical}")


def extract_availability(
    product_description: str, site_text: str, source_url: str, units: dict | None = None
) -> Availability:
    """Единственная публичная функция модуля. Никогда не бросает
    исключение — недоступность LLM/сеть/страница оказалась не той карточкой
    товара (is_product_page=False) -> Availability(status=UNKNOWN,
    evidence=None, ...), не выдумываем результат (тот же принцип, что у
    relevance_llm.classify_stock_status)."""
    units = units if units is not None else load_units()
    guess = relevance_llm.extract_availability(product_description, site_text)
    if guess is None or not guess.is_product_page:
        return _unknown(source_url)

    return Availability(
        status=_STATUS_MAP.get(guess.status, AvailabilityStatus.UNKNOWN),
        quantity=_quantity_from_guess(guess.quantity, guess.quantity_unit, units),
        pack_size=_quantity_from_guess(guess.pack_size_qty, guess.pack_size_unit, units),
        min_order=_quantity_from_guess(guess.min_order_qty, guess.min_order_unit, units),
        lead_time_days=guess.lead_time_days,
        price=guess.price,
        source_url=source_url,
        checked_at=datetime.now(),
        evidence=guess.evidence,
    )
