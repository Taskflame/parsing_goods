"""База доверенных поставщиков по категории закупок (design-обсуждение:
байер хочет, чтобы поиск нового товара сначала проверял уже проверенных
поставщиков той же категории, и только если их не хватило — подключал
сегодняшний глобальный поиск, см. pipeline._search_trusted_suppliers).

Единственное персистентное хранилище во всём проекте — до этого пайплайн
был полностью stateless (кроме reports/index.json в webapp.py, который
хранит только историю уже сделанных отчётов, не влияет на будущий
поиск). SQLite, а не YAML/JSON-файл (как остальные config/*.yaml) —
осознанный выбор: будущая аналитика вида "какой ранг реально выбирают
байеры" (см. ниже) — это SQL-запрос, а не ручной разбор JSON.

Домен — ключ поставщика, НЕ название компании: заголовок страницы из
поисковой выдачи нестабилен между прогонами (один и тот же сайт сегодня
приходит как "Генератор бензиновый Huter DY3000L", завтра как "Купить
генераторы Huter — официальный сайт"), домен стабилен и есть всегда, раз
результат вообще существует (см. scoring.company_domain). ИНН пишется
рядом, когда резолвился, но НЕ обязателен — при текущем качестве
обогащения (см. enrichment.py) это отбросило бы большую часть выдачи.

category_suppliers — журнал (append-only), не таблица "домен -> одна
категория": один и тот же домен может закрепиться за несколькими
категориями со временем, и КАЖДОЕ появление в топ-N (см.
pipeline._TRUSTED_SUPPLIERS_WRITE_BACK_TOP_N) — отдельная строка с рангом
и запросом, а не перезапись предыдущей. Это специально ради будущей
проверки гипотезы "действительно ли топ-1 был лучшим выбором, или байер
регулярно выбирал второй/третий вариант" — топ-1 такую проверку сделать
в принципе не позволяет, а история рангов — позволяет.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "trusted_suppliers.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    domain TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    inn TEXT,
    phone TEXT,
    email TEXT,
    address TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_code TEXT NOT NULL,
    domain TEXT NOT NULL,
    rank INTEGER NOT NULL,
    source_query TEXT NOT NULL,
    added_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_category_suppliers_category
    ON category_suppliers(category_code);
"""


class TrustedSupplierStore:
    """Обёртка над одним SQLite-файлом. Соединение открывается в
    конструкторе и держится до close() — вызывающий код (pipeline.py)
    открывает store на время одного search_and_score и закрывает в
    конце, отдельного пула соединений не заводим (не веб-сервер с
    конкурентным доступом к самому файлу БД, а один пайплайн-вызов)."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TrustedSupplierStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def domains_for_category(self, category_code: str) -> list[str]:
        """Все домены, хоть раз попавшие в топ-N для этой категории —
        без дедупа по частоте появления (её ранжирование — на будущее,
        когда данных накопится достаточно, чтобы это имело смысл; см.
        докстринг модуля про то, зачем category_suppliers — журнал, а не
        таблица с уникальным доменом на категорию)."""
        cursor = self._conn.execute(
            "SELECT DISTINCT domain FROM category_suppliers WHERE category_code = ?",
            (category_code,),
        )
        return [row[0] for row in cursor.fetchall()]

    def record_supplier(
        self,
        domain: str,
        name: str,
        category_code: str,
        rank: int,
        source_query: str,
        inn: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        address: str | None = None,
    ) -> None:
        """Обновляет/создаёт запись в suppliers (последнее известное имя/
        контакты, ИНН не затирается None, если уже был известен раньше —
        COALESCE) и ВСЕГДА добавляет новую строку в category_suppliers,
        даже если этот домен уже писался под этой категорией раньше (см.
        докстринг модуля)."""
        today = date.today().isoformat()
        now = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            """
            INSERT INTO suppliers (domain, name, inn, phone, email, address, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                name = excluded.name,
                inn = COALESCE(excluded.inn, suppliers.inn),
                phone = COALESCE(excluded.phone, suppliers.phone),
                email = COALESCE(excluded.email, suppliers.email),
                address = COALESCE(excluded.address, suppliers.address),
                last_seen = excluded.last_seen
            """,
            (domain, name, inn, phone, email, address, today, today),
        )
        self._conn.execute(
            """
            INSERT INTO category_suppliers (category_code, domain, rank, source_query, added_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (category_code, domain, rank, source_query, now),
        )
        self._conn.commit()

    def get_supplier(self, domain: str) -> dict | None:
        """Для тестов/отладки — карточка поставщика по домену, или None,
        если такого домена в базе ещё нет."""
        cursor = self._conn.execute(
            "SELECT domain, name, inn, phone, email, address, first_seen, last_seen "
            "FROM suppliers WHERE domain = ?",
            (domain,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [c[0] for c in cursor.description]
        return dict(zip(columns, row))
