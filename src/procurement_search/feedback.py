"""Обратная связь — ИЗОЛИРОВАННЫЙ модуль (не трогает поисковый пайплайн).

Возможности:
1. send_report_email(report_path, query, smtp_cfg) — отправляет готовый Excel-отчёт
   на почту через SMTP Yandex (smtp.yandex.ru). В письме — крупные HTML-кнопки
   👍/👎 со ссылкой на /api/feedback/submit?report_key=...&rating=up|down&email=<получатель>,
   чтобы человек мог оценить результат прямо из письма, и мы сразу знаем кто.
   Каждый получатель получает отдельное письмо с персональной ссылкой.
2. save_feedback / get_feedback_list — хранение отзывов в SQLite (feedback.db).

Хранение: FeedbackStore (SQLite, data/feedback.db), таблица feedback с полями
  report_key, query, name, email, rating ('up'/'down'/'text'), comment, created_at.
Каждое нажатие на эмодзи/текстовый отзыв — отдельная строка: видно кто (email),
по какому запросу (query) и с какой оценкой/комментарием.

Ничего не импортирует из webapp — функции принимают необходимое аргументами,
webapp передаёт пути/значения, избегая круговой зависимости.
"""

from __future__ import annotations

import logging
import os
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

logger = logging.getLogger(__name__)

# SQLite-файл с отзывами — рядом с trusted_suppliers.db (data/ в корне проекта).
# Отдельная БД, а не in-memory/index.json: пользователь хочет, чтобы каждый
# отзыв (кто, по какому запросу, оценка, комментарий) надёжно хранился.
DEFAULT_FEEDBACK_DB = Path(__file__).resolve().parents[2] / "data" / "feedback.db"

_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_key TEXT NOT NULL,
    query TEXT,
    name TEXT,
    email TEXT,
    rating TEXT NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_report_key ON feedback(report_key);
CREATE INDEX IF NOT EXISTS idx_feedback_email ON feedback(email);
"""


class FeedbackStore:
    """Обёртка над SQLite-файлом с отзывами (аналог TrustedSupplierStore).

    Каждая запись — один отзыв: на какой отчёт (report_key), по какому запросу
    (query), кто оставил (name, email), оценка (up/down/text) и комментарий.
    """

    def __init__(self, db_path: str | Path = DEFAULT_FEEDBACK_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_FEEDBACK_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "FeedbackStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def add(self, report_key: str, query: str | None, name: str | None,
            email: str | None, rating: str, comment: str = "") -> dict:
        # name/email необязательны: пустая строка/None = анонимный отзыв
        # (пользователь может оставить реакцию без ввода своих данных).
        clean_name = (name or "").strip() or None
        clean_email = (email or "").strip().lower() or None
        self._conn.execute(
            "INSERT INTO feedback (report_key, query, name, email, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (report_key, query, clean_name, clean_email, rating, (comment or "").strip(), _utcnow()),
        )
        self._conn.commit()
        return {"report_key": report_key, "query": query, "name": clean_name,
                "email": clean_email, "rating": rating, "comment": (comment or "").strip(),
                "created_at": _utcnow()}

    def list_all(self, since: str | None = None, until: str | None = None) -> list[dict]:
        """Все отзывы (опционально в диапазоне дат `since`..`until`).

        `since`/`until` — ISO-строки даты/времени (например "2026-09-01" или
        "2026-09-28T00:00:00+00:00"). created_at хранится как ISO-строка с
        timespec=seconds в UTC, поэтому сравнение строк лексикографически
        корректно для дат одного формата. Усекаем to-date до конца суток,
        чтобы `until="2026-09-28"` включал весь день, а не только полночь.
        """
        where = []
        params: list[str] = []
        if since:
            where.append("created_at >= ?")
            params.append(_to_iso_start(since))
        if until:
            where.append("created_at <= ?")
            params.append(_to_iso_end(until))
        query = "SELECT report_key, query, name, email, rating, comment, created_at FROM feedback"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY id DESC"
        rows = self._conn.execute(query, params).fetchall()
        cols = ["report_key", "query", "name", "email", "rating", "comment", "created_at"]
        return [dict(zip(cols, r)) for r in rows]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_dt(value: str) -> datetime:
    """ISO-строка (дата или дата+время) -> aware datetime UTC.

    Даты без времени ("2026-09-28") интерпретируются как полночь UTC.
    Научный парсинг naive/aware-строк — datetime.fromisoformat(); naive
    результаты приводятся к UTC, поскольку created_at хранится в UTC.
    """
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        # fallback для коротких дат
        dt = datetime.fromisoformat(f"{value}T00:00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_iso_start(value: str) -> str:
    """Начало суток ISO-строки, чтобы `since="2026-09-28"` значил "с 00:00"."""
    return _parse_dt(value).isoformat(timespec="seconds")


def _to_iso_end(value: str) -> str:
    """Конец суток ISO-строки (до конца дня), чтобы `until="2026-09-28"`
    включал отзывы за весь день, а не только до полночи."""
    dt = _parse_dt(value)
    day_end = dt.replace(hour=23, minute=59, second=59)
    return day_end.isoformat(timespec="seconds")


# --- Конфигурация почты (SMTP Yandex) ---------------------------------

def load_smtp_config() -> dict | None:
    """Собирает SMTP-конфиг из окружения. Возвращает None, если почта не
    настроена (тогда авторассылку молча пропускаем, не ломая поиск)."""
    host = os.environ.get("EMAIL_SMTP_HOST") or "smtp.yandex.ru"
    port = int(os.environ.get("EMAIL_SMTP_PORT") or "465")
    user = os.environ.get("EMAIL_FROM")
    password = os.environ.get("EMAIL_FROM_PASSWORD")
    to = os.environ.get("EMAIL_TO")
    if not (user and password and to):
        logger.info("feedback: почта не настроена (нет EMAIL_FROM/PASSWORD/TO) — отправка пропущена")
        return None
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_name": os.environ.get("EMAIL_FROM_NAME") or user,
        "to": to,
        "base_url": (os.environ.get("APP_BASE_URL") or "").rstrip("/"),
    }


def _rating_link(base_url: str, report_key: str, rating: str,
                 recipient: str | None = None) -> str:
    """Собирает ссылку оценки отчёта для письма.

    `recipient` — email получателя, кладётся в query-параметр `email`, чтобы
    по клику мы сразу знали, кто именно оценил (и не просили вводить email).
    URL кодирует email через urllib.parse.quote — адреса безопасны в URL.
    """
    from urllib.parse import urlencode
    params = {"report_key": report_key, "rating": rating}
    if recipient:
        params["email"] = recipient
    return f"{base_url}/api/feedback/submit?{urlencode(params)}"


def _build_attachment(report_path: Path) -> list["MIMEBase"]:
    """Прикрепляет Excel-отчёт как MIME-вложение (список из одной части).

    Возвращает [] при ошибке — вызов продолжает работу без вложения. Задаём
    правильный Content-Type для .xlsx, чтобы почтовик открыл файл в Excel.
    """
    import email.encoders as _enc
    from email.mime.base import MIMEBase

    mime = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if report_path.suffix.lower() == ".xlsx"
        else "application/octet-stream"
    )
    main, sub = mime.split("/", 1)
    part = MIMEBase(main, sub)
    part.set_payload(report_path.read_bytes())
    _enc.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=report_path.name)
    return [part]


def _send(smtp: dict, recipient: str, msg) -> None:
    """Отправляет одно письмо через SMTP. База и STARTTLS — по порту."""
    if smtp["port"] == 465:
        server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=30)
    else:
        server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=30)
        server.starttls()
    with server:
        server.login(smtp["user"], smtp["password"])
        server.sendmail(smtp["user"], [recipient], msg.as_string())
    logger.info("feedback: отчёт отправлен на %s", recipient)


def send_report_email(report_path: Path, query: str, smtp: dict | None = None,
                      to: str | list[str] | None = None) -> bool:
    """Отправляет Excel-отчёт на почту (SMTP). Возвращает True, если отправлено.

    `to` — получатель(и) письма: строка или список адресов. Если не задан —
    берётся `EMAIL_TO` из конфигурации (load_smtp_config).

    Дополнительно НЕ блокирует: при любой ошибке логирует и возвращает False —
    поиск завершается нормально, чтобы сбой почты не ломал отдачу результата.
    """
    if smtp is None:
        smtp = load_smtp_config()
    if smtp is None:
        return False
    if not report_path or not report_path.is_file():
        logger.warning("feedback: файл отчёта %s не найден — письмо не отправлено", report_path)
        return False

    # Нормализуем получателей к списку. Пустой после нормализации — не отправляем.
    recipients = [to] if isinstance(to, str) else (list(to) if to else [])
    if not recipients:
        cfg_to = smtp.get("to", "")
        recipients = [a.strip() for a in cfg_to.split(",") if a.strip()]
    if not recipients:
        logger.warning("feedback: не указан получатель (EMAIL_TO или параметр to) — не отправлено")
        return False

    subject = f"Отчёт по запросу: {query[:120]}"
    base = smtp.get("base_url", "")
    # Тело письма сначала в HTML — это часть, которую пользователь будет
    # настраивать под себя (эмодзи-кнопки). Текстовый вариант не содержит
    # сырых URL (в почте они выглядят некрасиво), только наброски.
    body_txt = (
        "Здравствуйте!\n\n"
        f"По вашему запросу \"{query}\" сформирован отчёт (во вложении).\n\n"
        "Оцените результат кнопками в письме (👍 или 👎).\n"
        "Узнать, что можно улучшить, можно по ссылке «Оставить комментарий».\n\n"
        "— Systeme Electric Procurement"
    )
    body_html_tpl = """<html><body style="font-family:Arial,sans-serif;color:#30332f;background:#f6f8f7;padding:20px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr><td align="center">
          <table role="presentation" style="max-width:520px;background:#ffffff;border-radius:14px;padding:24px;border:1px solid #e3e7e3">
            <tr><td>
              <p style="margin:0 0 12px;font-size:15px">Здравствуйте!</p>
              <p style="margin:0 0 18px;font-size:14px;color:#65655f">По запросу <b>"{query}"</b> сформирован отчёт (во вложении).</p>
              <p style="margin:0 0 20px;font-size:14px"><b>Помог ли результат?</b></p>
              <p style="margin:0;text-align:center">
                <!-- Кликабельные эмодзи: ведут на страницу с благодарностью, оценка пишется в БД -->
                <a href="{up}" style="display:inline-block;font-size:34px;text-decoration:none;padding:14px 22px;border-radius:12px;border:1px solid #e3e7e3;margin-right:12px;background:#f0fdf8">👍</a>
                <a href="{down}" style="display:inline-block;font-size:34px;text-decoration:none;padding:14px 22px;border-radius:12px;border:1px solid #e3e7e3;background:#fff4f4">👎</a>
              </p>
              <p style="margin:18px 0 0;text-align:center">
                <!-- Комментарий рядом с эмодзи: ведёт на форму комментария -->
                <a href="{comment_url}" style="display:inline-block;font-size:14px;text-decoration:none;padding:11px 20px;border-radius:12px;border:1px solid #e3e7e3;background:#eef6ff;color:#2e669d">💬 Оставить комментарий</a>
              </p>
              <p style="margin:18px 0 0;font-size:12px;color:#9aa09a">Нажмите на эмодзи, чтобы сразу оставить оценку. Если хотите дополнить — нажмите «Оставить комментарий».</p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body></html>"""

    # Каждый получатель получает отдельное письмо со своей персональной
    # ссылкой (в URL зашит его email) — так по клику сразу понятно, кто
    # оценил. Прикладываем вложение к каждому письму отдельно.
    from urllib.parse import urlencode
    attachment_parts = _build_attachment(report_path)
    for recipient in recipients:
        rcp = recipient.strip()
        up = _rating_link(base, report_path.name, "up", rcp)
        down = _rating_link(base, report_path.name, "down", rcp)
        _comment_params = {"report_key": report_path.name}
        if rcp:
            _comment_params["email"] = rcp
        comment_url = f"{base}/api/feedback/comment?{urlencode(_comment_params)}"
        body_html = body_html_tpl.format(query=query, up=up, down=down, comment_url=comment_url)

        msg = MIMEMultipart()
        msg["From"] = formataddr((smtp["from_name"], smtp["user"]))
        msg["To"] = rcp
        msg["Subject"] = subject
        msg.attach(MIMEText(body_txt, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        for part in attachment_parts:
            msg.attach(part)
        try:
            _send(smtp, rcp, msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("feedback: сбой отправки на %s: %s", rcp, exc)
            continue
    return True


def try_send_report_email(report_path: Path, query: str,
                          to: str | list[str] | None = None) -> bool:
    """Безопасная обёртка над send_report_email для webapp-эндпоинтов.

    Никогда не бросает исключений: при любой ошибке логирует и возвращает
    False, чтобы сбой/ненастройка почты не ломали отдачу результата поиска.
    Вызывается в /api/search и /api/photo-search (автоматическая рассылка
    каждого сформированного Excel-отчёта). `to` — персональный получатель
    (если пользователь указал свою почту), иначе отчёт уходит на EMAIL_TO.
    """
    try:
        return send_report_email(report_path, query, to=to)
    except Exception:  # noqa: BLE001
        logger.warning("feedback: автопочта отчёта %s не сработала", report_path.name, exc_info=True)
        return False


# --- Хранение фидбека в SQLite (feedback.db) -----------------------------

def save_feedback(
    report_key: str,
    name: str | None,
    email: str | None,
    rating: str,
    comment: str = "",
    *,
    query: str | None = None,
    db_path: str | Path | None = None,
) -> dict:
    # db_path разрешается в момент ВЫЗОВА (а не в момент определения функции),
    # иначе тесты/подмены DEFAULT_FEEDBACK_DB через monkeypatch не работали бы:
    # значение в сигнатуре по умолчанию фиксируется при импорте.
    if db_path is None:
        db_path = DEFAULT_FEEDBACK_DB
    """Записывает отзыв в SQLite (FeedbackStore).

    Рейтинг: 'up' (👍), 'down' (👎) или 'text' (только комментарий). Сохраняет
    report_key (на какой отчёт), query (по какому запросу), name/email (кто —
    ОБА необязательны), rating, comment и created_at.

    name/email могут быть пустыми/None — это анонимный отзыв (пользователь
    просто поставил реакцию, не вводя своих данных). Если email всё же задан,
    он должен быть валидным (содержать '@'); пустое имя допустимо.
    """
    if rating not in ("up", "down", "text"):
        return {"ok": False, "error": f"Неизвестный рейтинг: {rating!r}"}
    if email and "@" not in email:
        return {"ok": False, "error": "Email указан некорректно"}

    try:
        with FeedbackStore(db_path) as store:
            entry = store.add(
                report_key=report_key, query=query, name=name,
                email=email, rating=rating, comment=comment,
            )
    except sqlite3.Error as exc:
        logger.warning("feedback: сбой записи в БД: %s", exc)
        return {"ok": False, "error": "Не удалось сохранить отзыв (БД занята)"}
    return {"ok": True, "report_key": report_key, "feedback": entry}


def get_feedback_list(
    db_path: str | Path | None = None,
    report_key: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Плоский список отзывов из SQLite (опционально по отчёту и/или датам).

    db_path разрешается в момент вызова (а не при импорте) — чтобы подмена
    DEFAULT_FEEDBACK_DB в тестах/окружении работала.
    """
    if db_path is None:
        db_path = DEFAULT_FEEDBACK_DB
    try:
        with FeedbackStore(db_path) as store:
            rows = store.list_all(since=since, until=until)
    except sqlite3.Error as exc:
        logger.warning("feedback: сбой чтения БД: %s", exc)
        return []
    if report_key:
        return [r for r in rows if r.get("report_key") == report_key]
    return rows