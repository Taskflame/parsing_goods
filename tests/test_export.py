from datetime import date, datetime

import openpyxl

from procurement_search.attribute_extractor import Quantity
from procurement_search.export import export_companies_to_excel
from procurement_search.models import Availability, AvailabilityStatus, Company, FieldValue
from procurement_search.quantity_match import Verdict


def _company(name: str, with_availability: bool = False) -> Company:
    company = Company(
        inn=None, ogrn=None, name=FieldValue(name, "test", date(2026, 1, 1)), status="неизвестно"
    )
    if with_availability:
        company.availability = Availability(
            status=AvailabilityStatus.IN_STOCK_QTY,
            quantity=Quantity(10.0, "шт", "10 шт"),
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url="https://x.example/product",
            checked_at=datetime(2026, 8, 6, 12, 0),
            evidence="в наличии: 10 шт",
        )
        company.availability_verdict = Verdict.NOT_ENOUGH.value
        company.availability_verdict_text = "Недостаточно — есть всего 10 шт из 11"
    return company


def test_export_without_availability_keeps_header_at_row_one(tmp_path):
    output = export_companies_to_excel([_company("ООО Тест")], tmp_path / "out.xlsx")

    wb = openpyxl.load_workbook(output)
    ws = wb.active
    assert ws.cell(row=1, column=1).value == "Компания"
    assert ws.cell(row=2, column=1).value == "ООО Тест"


def test_export_with_summary_shifts_header_down_and_writes_summary_row(tmp_path):
    output = export_companies_to_excel(
        [_company("ООО Тест")],
        tmp_path / "out.xlsx",
        required_qty=Quantity(11.0, "шт", "11 шт"),
        summary="Требуется: 11 шт\nПодтверждено на складах: 10 шт у 1 поставщиков",
    )

    wb = openpyxl.load_workbook(output)
    ws = wb.active
    assert "Требуется: 11 шт" in ws.cell(row=1, column=1).value
    assert ws.cell(row=2, column=1).value == "Компания"
    assert ws.cell(row=3, column=1).value == "ООО Тест"


def test_export_writes_availability_columns():
    """Индексы новых колонок (15-22, см. export._COL_*): "Запрошено",
    "Найдено", ..., "Наличие (кол-во): источник/дата"."""
    from procurement_search.export import (
        _COL_AVAILABILITY_EVIDENCE,
        _COL_AVAILABILITY_SOURCE,
        _COL_AVAILABILITY_VERDICT,
        _COL_FOUND_QTY,
        _COL_REQUESTED,
    )

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        output = export_companies_to_excel(
            [_company("ООО Тест", with_availability=True)],
            Path(tmp) / "out.xlsx",
            required_qty=Quantity(11.0, "шт", "11 шт"),
        )
        wb = openpyxl.load_workbook(output)
        ws = wb.active

        assert ws.cell(row=2, column=_COL_REQUESTED).value == "11 шт"
        assert ws.cell(row=2, column=_COL_FOUND_QTY).value == "10 шт"
        assert ws.cell(row=2, column=_COL_AVAILABILITY_VERDICT).value == (
            "Недостаточно — есть всего 10 шт из 11"
        )
        assert ws.cell(row=2, column=_COL_AVAILABILITY_EVIDENCE).value == "в наличии: 10 шт"
        assert "x.example" in ws.cell(row=2, column=_COL_AVAILABILITY_SOURCE).value


def test_export_without_availability_leaves_new_columns_blank(tmp_path):
    from procurement_search.export import _COL_AVAILABILITY_VERDICT, _COL_FOUND_QTY

    output = export_companies_to_excel([_company("ООО Тест")], tmp_path / "out.xlsx")

    wb = openpyxl.load_workbook(output)
    ws = wb.active
    # openpyxl не различает "" и пустую ячейку при перечитывании файла —
    # обе читаются как None, хотя записывались пустой строкой (см.
    # export.py: "" для отсутствующих полей, тот же принцип, что и у
    # остальных пустых колонок в этом экспортере).
    assert ws.cell(row=2, column=_COL_FOUND_QTY).value is None
    assert ws.cell(row=2, column=_COL_AVAILABILITY_VERDICT).value is None
