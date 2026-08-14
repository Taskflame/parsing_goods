"""Шаг [7] пайплайна: экспорт в Excel (design_doc §8).

Принцип "фифти-фифти" реализован буквально: для каждого контактного поля
экспортируется не только значение, но источник, дата получения и флаг
достоверности — так байер видит, какие именно ячейки требуют проверки,
вместо перепроверки всей строки.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

from procurement_search.models import Company, VerificationFlag

_HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True)

_FLAG_FILL = {
    VerificationFlag.CONFIRMED: PatternFill(
        start_color="D1FAE5", end_color="D1FAE5", fill_type="solid"
    ),
    VerificationFlag.UNVERIFIED: PatternFill(
        start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"
    ),
    VerificationFlag.STALE: PatternFill(
        start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"
    ),
}

_COLUMNS = [
    ("Компания", 32),
    ("ИНН", 14),
    ("Статус", 18),
    ("Телефон", 20),
    ("Телефон: источник/дата", 26),
    ("Email", 24),
    ("Email: источник/дата", 26),
    ("Адрес", 30),
    ("Адрес: источник/дата", 26),
    ("Сайт", 26),
    ("Сайт: источник/дата", 26),
    ("Источники (каталоги)", 20),
    ("Score: релевантность", 12),
    ("Score: доверие", 12),
    ("Score: полнота данных", 14),
    ("Score: итого", 12),
]


def _field_summary(field_values) -> tuple[str, str, VerificationFlag]:
    if not field_values:
        return "", "", VerificationFlag.UNVERIFIED
    fv = field_values[0]
    return fv.value, f"{fv.source}, {fv.retrieved_at.isoformat()}", fv.confidence


def _write_header(ws: Worksheet) -> None:
    for col_idx, (title, width) in enumerate(_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=title)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = "A2"


def export_companies_to_excel(companies: list[Company], output_path: str | Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Поставщики"
    _write_header(ws)

    # ранжирование по итоговому score, если посчитан
    ordered = sorted(
        companies, key=lambda c: c.score.total if c.score else 0.0, reverse=True
    )

    for row_idx, company in enumerate(ordered, start=2):
        phone_val, phone_src, phone_flag = _field_summary(company.contacts.get("phone", []))
        email_val, email_src, email_flag = _field_summary(company.contacts.get("email", []))
        addr_val, addr_src, addr_flag = _field_summary(company.contacts.get("address", []))
        site_val, site_src, site_flag = _field_summary(company.contacts.get("website", []))

        score = company.score
        row = [
            company.name.value,
            company.inn or "",
            company.status,
            phone_val,
            phone_src,
            email_val,
            email_src,
            addr_val,
            addr_src,
            site_val,
            site_src,
            ", ".join(company.sources),
            round(score.relevance, 3) if score else "",
            round(score.trust, 3) if score else "",
            round(score.confidence, 3) if score else "",
            round(score.total, 3) if score else "",
        ]
        for col_idx, value in enumerate(row, start=1):
            ws.cell(row=row_idx, column=col_idx, value=value)

        for col_idx, flag in ((4, phone_flag), (6, email_flag), (8, addr_flag), (10, site_flag)):
            ws.cell(row=row_idx, column=col_idx).fill = _FLAG_FILL[flag]

    output_path = Path(output_path)
    wb.save(output_path)
    return output_path
