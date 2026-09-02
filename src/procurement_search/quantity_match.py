"""Сравнение требуемого количества закупки с найденным остатком (см.
attribute_extractor.ParsedQuery.order_qty / availability.Availability).

`compare` — единственная публичная функция: чистая, без сети/LLM, поэтому
дешёвая и вызывается на каждого top-N кандидата после availability.extract_availability
(см. pipeline.py). Namedtuple-подобный результат (Verdict, текст для Excel/
веб-таблицы) — вердикт используется дальше и для сортировки (VERDICT_SORT_ORDER),
и для решения "выкинуть ли из выдачи" (только OUT_OF_STOCK, см. pipeline.py про
knockout-паттерн и обязательное логирование).

Приведение единиц — только через явную фасовку (Availability.pack_size),
никогда молча: "2 упаковки" не сравниваются напрямую с "11 шт" без знания,
сколько штук в упаковке. Тот же принцип "нет данных — не штраф"/"не
угадывать", что и везде в scoring.py/attribute_extractor.py — несопоставимые
единицы дают UNIT_MISMATCH, а не молчаливое приведение и не NOT_ENOUGH.
"""

from __future__ import annotations

from enum import Enum

from procurement_search.attribute_extractor import Quantity
from procurement_search.models import Availability, AvailabilityStatus


class Verdict(str, Enum):
    ENOUGH = "достаточно"
    NOT_ENOUGH = "недостаточно"
    IN_STOCK_NO_QTY = "в_наличии_количество_не_указано"
    ON_ORDER = "под_заказ"
    OUT_OF_STOCK = "нет_в_наличии"
    UNIT_MISMATCH = "единицы_не_сопоставимы"
    UNKNOWN = "нет_данных"


# Порядок сортировки внутри равного score (ТЗ): ENOUGH -> NOT_ENOUGH ->
# IN_STOCK_NO_QTY -> ON_ORDER -> UNIT_MISMATCH -> UNKNOWN -> OUT_OF_STOCK.
# Поставщик с частичным остатком — повод для звонка, а не отказа, поэтому
# он ранжируется ВЫШЕ поставщика без опубликованного остатка вообще
# (design-обсуждение: "10 из 11" — более действенная зацепка для байера,
# чем полное отсутствие данных).
VERDICT_SORT_ORDER: dict[Verdict, int] = {
    Verdict.ENOUGH: 0,
    Verdict.NOT_ENOUGH: 1,
    Verdict.IN_STOCK_NO_QTY: 2,
    Verdict.ON_ORDER: 3,
    Verdict.UNIT_MISMATCH: 4,
    Verdict.UNKNOWN: 5,
    Verdict.OUT_OF_STOCK: 6,
}

_TEMPLATES: dict[Verdict, str] = {
    Verdict.ENOUGH: "Достаточно — {found:g} {unit} (нужно {required:g})",
    Verdict.NOT_ENOUGH: "Недостаточно — есть всего {found:g} {unit} из {required:g}",
    Verdict.IN_STOCK_NO_QTY: "В наличии, количество не указано",
    Verdict.ON_ORDER: "Под заказ, срок {days} дней",
    Verdict.OUT_OF_STOCK: "Нет в наличии",
    Verdict.UNIT_MISMATCH: "Найдено {found:g} {found_unit} — не сопоставимо с {required:g} {unit}",
    Verdict.UNKNOWN: "Нет данных — уточнить у поставщика",
}


def convert_to_unit(found: Quantity, pack_size: Quantity | None, target_unit: str) -> float | None:
    """Переводит найденное количество в целевую единицу — только напрямую
    (единицы совпадают) или через явную фасовку (found — число упаковок,
    pack_size — штук/литров и т.п. в одной упаковке). Любой другой случай —
    None, вызывающий код трактует это как UNIT_MISMATCH, не пытаясь
    угадать курс пересчёта."""
    if found.unit == target_unit:
        return found.value
    if pack_size is not None and pack_size.unit == target_unit:
        return found.value * pack_size.value
    return None


def compare(required: Quantity | None, availability: Availability) -> tuple[Verdict, str]:
    """Возвращает вердикт и готовый текст для колонки Excel/веб-таблицы.

    Порядок проверок: сначала статус со страницы (нет в наличии/под заказ/
    нет данных — эти случаи не зависят от чисел вообще), затем — сравнение
    чисел, только если у обеих сторон (запрос и остаток) есть количество."""
    if availability.status == AvailabilityStatus.OUT_OF_STOCK:
        return Verdict.OUT_OF_STOCK, _TEMPLATES[Verdict.OUT_OF_STOCK]

    if availability.status == AvailabilityStatus.UNKNOWN:
        return Verdict.UNKNOWN, _TEMPLATES[Verdict.UNKNOWN]

    if availability.status == AvailabilityStatus.ON_ORDER:
        days = availability.lead_time_days
        if days is not None:
            text = _TEMPLATES[Verdict.ON_ORDER].format(days=days)
        else:
            text = "Под заказ"
        return Verdict.ON_ORDER, text

    if required is None or availability.quantity is None:
        return Verdict.IN_STOCK_NO_QTY, _TEMPLATES[Verdict.IN_STOCK_NO_QTY]

    converted = convert_to_unit(availability.quantity, availability.pack_size, required.unit)
    if converted is None:
        text = _TEMPLATES[Verdict.UNIT_MISMATCH].format(
            found=availability.quantity.value,
            found_unit=availability.quantity.unit,
            required=required.value,
            unit=required.unit,
        )
        return Verdict.UNIT_MISMATCH, text

    if converted >= required.value:
        text = _TEMPLATES[Verdict.ENOUGH].format(found=converted, unit=required.unit, required=required.value)
        return Verdict.ENOUGH, text

    text = _TEMPLATES[Verdict.NOT_ENOUGH].format(found=converted, unit=required.unit, required=required.value)
    return Verdict.NOT_ENOUGH, text
