from datetime import datetime

from procurement_search.attribute_extractor import Quantity
from procurement_search.models import Availability, AvailabilityStatus
from procurement_search.quantity_match import Verdict, compare

_NOW = datetime(2026, 1, 1)


def _availability(
    status=AvailabilityStatus.IN_STOCK_QTY,
    quantity=None,
    pack_size=None,
    lead_time_days=None,
):
    return Availability(
        status=status,
        quantity=quantity,
        pack_size=pack_size,
        min_order=None,
        lead_time_days=lead_time_days,
        price=None,
        source_url="https://example.com/product",
        checked_at=_NOW,
        evidence=None,
    )


def test_compare_enough_when_stock_exceeds_required():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(quantity=Quantity(15.0, "шт", "15 шт"))

    verdict, text = compare(required, availability)

    assert verdict == Verdict.ENOUGH
    assert text == "Достаточно — 15 шт (нужно 11)"


def test_compare_not_enough_when_stock_below_required():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(quantity=Quantity(10.0, "шт", "10 шт"))

    verdict, text = compare(required, availability)

    assert verdict == Verdict.NOT_ENOUGH
    assert text == "Недостаточно — есть всего 10 шт из 11"


def test_compare_not_enough_via_pack_size_conversion():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(
        quantity=Quantity(2.0, "упак", "2 упак"), pack_size=Quantity(5.0, "шт", "упаковка 5 шт")
    )

    verdict, text = compare(required, availability)

    assert verdict == Verdict.NOT_ENOUGH
    assert "10" in text and "11" in text


def test_compare_enough_via_pack_size_conversion():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(
        quantity=Quantity(3.0, "упак", "3 упак"), pack_size=Quantity(5.0, "шт", "упаковка 5 шт")
    )

    verdict, text = compare(required, availability)

    assert verdict == Verdict.ENOUGH
    assert "15" in text


def test_compare_unit_mismatch_without_known_pack_size():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(quantity=Quantity(2.0, "упак", "2 упак"), pack_size=None)

    verdict, text = compare(required, availability)

    assert verdict == Verdict.UNIT_MISMATCH
    assert text == "Найдено 2 упак — не сопоставимо с 11 шт"


def test_compare_in_stock_no_qty_when_availability_has_no_number():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(status=AvailabilityStatus.IN_STOCK, quantity=None)

    verdict, text = compare(required, availability)

    assert verdict == Verdict.IN_STOCK_NO_QTY
    assert text == "В наличии, количество не указано"


def test_compare_in_stock_no_qty_when_no_required_quantity():
    availability = _availability(quantity=Quantity(15.0, "шт", "15 шт"))

    verdict, text = compare(None, availability)

    assert verdict == Verdict.IN_STOCK_NO_QTY


def test_compare_out_of_stock():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(status=AvailabilityStatus.OUT_OF_STOCK)

    verdict, text = compare(required, availability)

    assert verdict == Verdict.OUT_OF_STOCK
    assert text == "Нет в наличии"


def test_compare_on_order_with_lead_time():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(status=AvailabilityStatus.ON_ORDER, lead_time_days=7)

    verdict, text = compare(required, availability)

    assert verdict == Verdict.ON_ORDER
    assert text == "Под заказ, срок 7 дней"


def test_compare_unknown_when_no_evidence_found():
    required = Quantity(11.0, "шт", "11 шт")
    availability = _availability(status=AvailabilityStatus.UNKNOWN)

    verdict, text = compare(required, availability)

    assert verdict == Verdict.UNKNOWN
    assert text == "Нет данных — уточнить у поставщика"
