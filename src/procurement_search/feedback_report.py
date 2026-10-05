"""Сбор отзывов/реакций в Excel и понедельный отчёт.

ИЗОЛИРОВАННЫЙ модуль (не трогает поисковый пайплайн) — решает ровно задачу
пользователя: "мы можем собирать все реакции и отзывы людей из сообщений в
отдельный файл — например excel — и собирать отчёт понедельно?"

Как устроено (сценарий пользователя):
1. Человек получает отчёт с кнопками 👍/👎 (feedback.send_report_email) и/или
   видит форму оценки на сайте — нажимает реакцию.
2. Оценка/комментарий сохраняется в SQLite feedback.db (feedback.FeedbackStore) —
   уже работает в webapp (api_feedback / api_feedback_submit / api_feedback_comment).
3. Этот модуль читает feedback.db и собирает ВСЕ реакции в один Excel-файл:
   лист "Все отзывы" (плоская таблица) + лист "Сводка по неделям"
   (+ x̄-плита недель) — то, что пользователь просил "понедельный отчёт".

Формат created_at в feedback.db — ISO-строка с timespec=seconds в UTC
("2026-09-28T07:00:00+00:00"). Дата/время сохраняются как есть; недели
группируются по дате создания (UTC), начало недели — понедельник.

Ничего не импортирует из webapp — всё принимает аргументами (db_path,
output_path, даты), избегая круговой зависимости, как и feedback.py.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

from procurement_search.feedback import FeedbackStore, DEFAULT_FEEDBACK_DB

logger = logging.getLogger(__name__)

# Получатель по умолчанию понедельного отчёта по отзывам. Если не задан
# EMAIL_TO в окружении — отчёт уходит сюда. Значение можно переопределить
# аргументом `to` в send_weekly_feedback_report.
DEFAULT_FEEDBACK_REPORT_RECIPIENT = "aleksandr.smurov@systeme.ru"

_RATING_LABEL = {"up": "👍 Всё устроило", "down": "👎 Не устроило", "text": "💬 Комментарий"}

# Одна палитра на оба листа — тот же стиль, что в export.py (тёмная шапка,
# зелёная/красная заливка по знаку), чтобы отчёт выглядел единообразно.
_HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_WEEK_FILL = PatternFill(start_color="EFF6FF", end_color="EFF6FF", fill_type="solid")
_UP_FILL = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
_DOWN_FILL = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
_TOTAL_FONT = Font(bold=True)

_ALL_COLUMNS = [
    ("Дата (UTC)", 20),
    ("Запрос", 34),
    ("Имя", 22),
    ("Email", 28),
    ("Оценка", 18),
    ("Комментарий", 50),
]

_SUMMARY_COLUMNS = [
    ("Неделя (Пн–Вс)", 14),
    ("Начало", 12),
    ("Конец", 12),
    ("Всего", 9),
    ("👍", 7),
    ("👎", 7),
    ("Комментарии", 14),
    ("Отзывов с текстом", 18),
]


def _parse_created(value: str) -> datetime:
    """ISO-строка created_at -> aware datetime UTC (см. feedback._parse_dt)."""
    from procurement_search.feedback import _parse_dt

    return _parse_dt(value)


def _week_start(dt: datetime) -> date:
    """Понедельник недели, к которой относится dt (агрегация по неделям)."""
    return (dt.date() - timedelta(days=dt.weekday()))


def group_by_week(rows: list[dict]) -> list[tuple[date, list[dict]]]:
    """Группирует отзывы по неделе начала (понедельник), сортирует по убыванию.

    Возвращает список (дата-понедельника, [отзыв, ...]). Записи, чью дату не
    удалось распарсить, пропускаются из агрегации (с предупреждением в лог) —
    не выдумываем неделю для битых данных.
    """
    buckets: dict[date, list[dict]] = defaultdict(list)
    for row in rows:
        try:
            dt = _parse_created(row["created_at"])
        except (ValueError, TypeError):
            logger.warning(
                "feedback_report: не удалось разобрать дату %r — запись пропущена в сводке",
                row.get("created_at"),
            )
            continue
        buckets[_week_start(dt)].append(row)
    return sorted(buckets.items(), key=lambda kv: kv[0], reverse=True)


def _style_header(ws: Worksheet, columns: list[tuple[str, int]]) -> None:
    """Заголовок листа: тёмная заливка, белый жирный текст, ширина колонок."""
    for idx, (title, width) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=idx, value=title)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = "A2"


def _write_all_sheet(ws: Worksheet, rows: list[dict]) -> None:
    """Лист "Все отзывы": плоская таблица, та же логика, что у UI."""
    _style_header(ws, _ALL_COLUMNS)
    for r, row in enumerate(rows, start=2):
        rating = _RATING_LABEL.get(row.get("rating"), row.get("rating", ""))
        values = [
            row.get("created_at", ""),
            row.get("query", ""),
            row.get("name", ""),
            row.get("email", ""),
            rating,
            row.get("comment", "") or "",
        ]
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            if c == 5 and row.get("rating") == "up":
                cell.fill = _UP_FILL
            elif c == 5 and row.get("rating") == "down":
                cell.fill = _DOWN_FILL
    ws.auto_filter.ref = f"A1:F{max(1, len(rows) + 1)}"


def _write_weekly_sheet(ws: Worksheet, weekly: list[tuple[date, list[dict]]]) -> None:
    """Лист "Сводка по неделям": одна строка на неделю с агрегацией реакций."""
    _style_header(ws, _SUMMARY_COLUMNS)
    for r, (start, week_rows) in enumerate(weekly, start=2):
        end = start + timedelta(days=6)
        up = sum(1 for x in week_rows if x.get("rating") == "up")
        down = sum(1 for x in week_rows if x.get("rating") == "down")
        with_text = sum(1 for x in week_rows if (x.get("comment") or "").strip())
        comments = sum(1 for x in week_rows if x.get("rating") == "text")
        values = [
            f'{start.strftime("%d.%m")} – {end.strftime("%d.%m")}',
            start.isoformat(),
            end.isoformat(),
            len(week_rows),
            up,
            down,
            comments,
            with_text,
        ]
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            if c == 1:
                cell.fill = _WEEK_FILL
            elif c == 5:
                cell.fill = _UP_FILL
            elif c == 6:
                cell.fill = _DOWN_FILL
    # Итоговая строка по всему диапазону.
    if weekly:
        total_row = len(weekly) + 2
        all_rows = [x for _, week in weekly for x in week]
        up = sum(1 for x in all_rows if x.get("rating") == "up")
        down = sum(1 for x in all_rows if x.get("rating") == "down")
        with_text = sum(1 for x in all_rows if (x.get("comment") or "").strip())
        comments = sum(1 for x in all_rows if x.get("rating") == "text")
        values = [
            "ИТОГО",
            "",
            "",
            len(all_rows),
            up,
            down,
            comments,
            with_text,
        ]
        for c, value in enumerate(values, start=1):
            cell = ws.cell(row=total_row, column=c, value=value)
            cell.font = _TOTAL_FONT
            cell.fill = _WEEK_FILL
    ws.auto_filter.ref = f"A1:H{max(1, len(weekly) + 2)}"


def export_feedback_to_excel(
    rows: list[dict],
    output_path: str | Path,
) -> Path:
    """Пишет отзывы/реакции в Excel-файл с двумя листами.

    - "Все отзывы" — плоский список (что видит UI);
    - "Сводка по неделям" — понедельная агрегация (+/‑/комментарии).

    Возвращает путь к записанному файлу. Сохраняет created_at как текст ISO —
    чтобы не зависеть от региональных настроек Excel при показе дат.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    all_ws = wb.active
    all_ws.title = "Все отзывы"
    _write_all_sheet(all_ws, rows)

    weekly = group_by_week(rows)
    weekly_ws = wb.create_sheet("Сводка по неделям")
    _write_weekly_sheet(weekly_ws, weekly)

    wb.save(output)
    logger.info(
        "feedback_report: записано %d отзывов в %s (%d недель)",
        len(rows), output, len(weekly),
    )
    return output


def build_feedback_report(
    output_path: str | Path,
    *,
    since: str | None = None,
    until: str | None = None,
    db_path: str | Path = DEFAULT_FEEDBACK_DB,
) -> Path:
    """Читает feedback.db и собирает Excel-отчёт по всем реакциям.

    since/until — ISO-даты (см. FeedbackStore.list_all): ограничивают, какие
    отзывы попадут в отчёт (например, since="2026-09-21" — за последнюю
    неделю). Если не заданы — выгружается весь накопленный фидбек.
    """
    with FeedbackStore(db_path) as store:
        rows = store.list_all(since=since, until=until)
    if not rows:
        logger.warning("feedback_report: в диапазоне %s..%s отзывов нет", since, until)
    return export_feedback_to_excel(rows, output_path)


def send_feedback_report_email(
    report_path: str | Path,
    *,
    since: str | None = None,
    until: str | None = None,
    to: str | list[str] | None = None,
) -> bool:
    """Отправляет готовый Excel-отчёт по отзывам на почту (SMTP).

    Переиспользует то же SMTP-соединение и парсер конфигурации, что и
    feedback.send_report_email (smtp.yandex.ru), но без HTML-кнопок оценки —
    отчёт-вложение приходит как есть. Получатель по умолчанию —
    DEFAULT_FEEDBACK_REPORT_RECIPIENT, если не задан `to` и нет EMAIL_TO
    в окружении.

    Никогда не бросает исключений: при любой ошибке логирует и возвращает
    False, чтобы сбой/ненастройка почты не роняли вызывающий код.
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr
    from datetime import datetime as _dt

    from procurement_search.feedback import (
        _build_attachment,
        _send,
        load_smtp_config,
    )

    smtp = load_smtp_config()
    if smtp is None:
        logger.warning("feedback_report: почта не настроена — отправка отчёта по отзывам пропущена")
        return False
    if not Path(report_path).is_file():
        logger.warning("feedback_report: файл отчёта %s не найден — письмо не отправлено", report_path)
        return False

    recipients = [to] if isinstance(to, str) else (list(to) if to else [])
    if not recipients:
        cfg_to = smtp.get("to", "")
        recipients = [a.strip() for a in cfg_to.split(",") if a.strip()]
    if not recipients:
        recipients = [DEFAULT_FEEDBACK_REPORT_RECIPIENT]
    if not recipients:
        logger.warning("feedback_report: не указан получатель — не отправлено")
        return False

    attachment_parts = _build_attachment(Path(report_path))
    stamp = _dt.now().strftime("%d.%m.%Y")
    subject = f"Отчёт по отзывам за {stamp}"
    body_txt = (
        "Здравствуйте!\n\n"
        f"Отчёт по реакциям и отзывам за период {since or 'всё время'} – "
        f"{until or 'сейчас'} сформирован (во вложении).\n\n"
        "— Systeme Electric Procurement"
    )

    sent = False
    for recipient in recipients:
        rcp = recipient.strip()
        msg = MIMEMultipart()
        msg["From"] = formataddr((smtp["from_name"], smtp["user"]))
        msg["To"] = rcp
        msg["Subject"] = subject
        msg.attach(MIMEText(body_txt, "plain", "utf-8"))
        for part in attachment_parts:
            msg.attach(part)
        try:
            _send(smtp, rcp, msg)
            sent = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("feedback_report: сбой отправки на %s: %s", rcp, exc)
    return sent


def send_weekly_feedback_report(
    *,
    db_path: str | Path = DEFAULT_FEEDBACK_DB,
    to: str | list[str] | None = None,
) -> bool:
    """Собирает понедельничный отчёт по отзывам за прошлую неделю и шлёт на почту.

    Неделя — с понедельника по воскресенье. Отчёт охватывает ПРЕДЫДУЩУЮ полную
    неделю (а не текущую в процессе), чтобы данные были за замкнутый период.
    Возвращает True, если письмо ушло хотя бы одному получателю.
    """
    import tempfile

    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_sunday = this_monday - timedelta(days=1)
    last_monday = last_sunday - timedelta(days=6)

    tmp_dir = Path(tempfile.mkdtemp(prefix="feedback_weekly_"))
    out = build_feedback_report(
        tmp_dir / f"feedback_{last_monday.isoformat()}_{last_sunday.isoformat()}.xlsx",
        since=last_monday.isoformat(),
        until=last_sunday.isoformat(),
        db_path=db_path,
    )
    sent = send_feedback_report_email(
        out,
        since=last_monday.isoformat(),
        until=last_sunday.isoformat(),
        to=to,
    )
    if sent:
        logger.info("feedback_report: понедельный отчёт отправлен за %s..%s", last_monday, last_sunday)
    return sent