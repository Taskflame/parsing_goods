"""Локальный веб-GUI поверх пайплайна (design_doc §8: удобный просмотр
выдачи и истории отчётов вместо CLI + открытия Excel).

Запуск: `PYTHONPATH=src python3 -m procurement_search.webapp`, затем
открыть http://127.0.0.1:8000 в браузере. FastAPI-бэкенд отдаёт JSON под
`/api/*`, статический фронтенд (static/index.html, ванильный JS без
сборки) обращается к нему через fetch.

Отчёты (Excel + метаданные для истории) складываются в `reports/` в корне
проекта — это то же самое, что делает CLI (`run_pipeline`), просто с
сохранением истории поисков между запусками.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from procurement_search.config import load_categories
from procurement_search.export import export_companies_to_excel
from procurement_search.log_explainer import ExplainingLogHandler
from procurement_search.models import Company, FieldValue, LEGAL_ADDRESS
from procurement_search.pipeline import search_and_score, summarize_availability
from procurement_search.trusted_suppliers import TrustedSupplierStore

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_ROOT / "reports"
INDEX_PATH = REPORTS_DIR / "index.json"
STATIC_DIR = PROJECT_ROOT / "static"
LOGS_DIR = PROJECT_ROOT / "logs"

app = FastAPI(title="Procurement Search")


class SearchRequest(BaseModel):
    query: str
    use_llm_fallback: bool = False
    deep_relevance: bool = False
    relevance_llm_check: bool = False
    use_trusted_suppliers: bool = False
    check_availability: bool = False
    probe_stepper: bool = False


class AddTrustedSupplierRequest(BaseModel):
    """Ручное добавление в trusted_suppliers.py — пока автопоиск/write-back
    (pipeline._write_back_trusted_suppliers) недоступен (см. 403 от Yandex
    Cloud, design-обсуждение) или просто когда байер точно знает
    поставщика заранее и не хочет ждать, пока тот сам всплывёт в топ-N
    какого-то будущего поиска."""

    domain: str
    name: str
    category_code: str
    inn: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None


# Ручные добавления через UI — не результат реального поиска, поэтому не
# могут честно иметь ранг/запрос из настоящей выдачи (см.
# TrustedSupplierStore.record_supplier: rank/source_query обязательны, это
# журнал КАК он был найден). rank=1 — вручную добавленный поставщик, по
# определению, уже проверен байером лично, ставим его в приоритет перед
# ещё не проверенными автоматическими находками той же категории.
# source_query — не настоящий поисковый запрос, а маркер происхождения
# записи, чтобы отличать ручные добавления от автоматических при разборе
# истории (см. категория_suppliers.source_query в trusted_suppliers.py).
_MANUAL_ADD_RANK = 1
_MANUAL_ADD_SOURCE_QUERY = "добавлено вручную через UI"

# Схема/www/путь — байер может вставить домен как угодно ("https://
# example.ru/catalog", "www.example.ru", "example.ru") — приводим к
# голому домену, тому же формату, что и у автоматически найденных (см.
# scoring.company_domain, та же идея, но без готового Company-объекта —
# _WEBSITE_DOMAIN_RE там ожидает "http(s)://" в начале, а тут поле формы
# может быть и без схемы вовсе).
_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/\s]+)", re.IGNORECASE)


def _normalize_domain(raw: str) -> str:
    match = _DOMAIN_RE.match(raw.strip())
    return (match.group(1) if match else raw.strip()).lower()


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w]+", "_", text.strip().lower(), flags=re.UNICODE).strip("_")
    return slug[:max_len] or "query"


# FastAPI выполняет sync-хендлеры (def, не async def) в общем threadpool
# внутри одного процесса uvicorn (см. README §Деплой — несколько
# одновременных пользователей на сервере) — GIL НЕ защищает от гонки
# между потоками сам по себе: он не даёт двум потокам выполнять байткод
# одновременно, но "прочитать файл → изменить список в памяти →
# записать файл" — это десятки байткод-инструкций, между любыми двумя из
# которых GIL может переключить поток. Без явного лока два одновременных
# поиска могут прочитать один и тот же index.json, каждый вставить свою
# запись и переписать файл целиком — тогда запись того, кто сохранил
# первым, бесследно теряется (сам .xlsx на диске остаётся, но выпадает
# из истории). Лок сериализует read-modify-write целиком.
_REPORT_INDEX_LOCK = threading.Lock()


def _load_report_index_unlocked() -> list[dict]:
    if not INDEX_PATH.exists():
        return []
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def _save_report_index_unlocked(entries: list[dict]) -> None:
    # os.replace — атомарная замена файла на уровне ОС (rename одного
    # inode на другой): читатель увидит либо старую, либо новую версию
    # файла целиком, никогда — обрывок. Защищает от битого JSON, если
    # процесс упадёт/будет убит ровно посреди записи (OOM-killer, docker
    # restart, деплой) — лок от этого не спасает, он только про порядок
    # доступа внутри процесса, а не про атомарность самой записи на диск.
    tmp_path = INDEX_PATH.with_name(INDEX_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, INDEX_PATH)


def _load_report_index() -> list[dict]:
    with _REPORT_INDEX_LOCK:
        return _load_report_index_unlocked()


def _append_report_entry(entry: dict) -> None:
    with _REPORT_INDEX_LOCK:
        entries = _load_report_index_unlocked()
        entries.insert(0, entry)
        _save_report_index_unlocked(entries)


def _preferred_display_value(field_values: list[FieldValue]) -> FieldValue | None:
    """Первое значение поля для превью — для адреса это фактический адрес
    работы, а не юридический из ЕГРЮЛ (см. _field_summary в export.py: у
    kazan.geogrunt.ru юр.адрес в Барнауле, а офис в Казани). Для остальных
    полей — просто первое значение."""
    if not field_values:
        return None
    if getattr(field_values[0], "kind", None) == LEGAL_ADDRESS:
        for fv in field_values:
            if getattr(fv, "kind", None) != LEGAL_ADDRESS:
                return fv
    return field_values[0]


def _field_to_dict(field_values: list[FieldValue]) -> dict | None:
    fv = _preferred_display_value(field_values)
    if fv is None:
        return None
    return {
        "value": fv.value,
        "source": fv.source,
        "date": fv.retrieved_at.isoformat(),
        "confidence": fv.confidence.value,
        "kind": fv.kind,
    }


def _single_field_to_dict(fv: FieldValue | None) -> dict | None:
    """Как _field_to_dict, но для одиночного поля (не списка) — см.
    Company.price в models.py."""
    if fv is None:
        return None
    return {
        "value": fv.value,
        "source": fv.source,
        "date": fv.retrieved_at.isoformat(),
        "confidence": fv.confidence.value,
    }


def _company_to_dict(company: Company) -> dict:
    score = company.score
    return {
        "name": company.name.value,
        "inn": company.inn,
        "status": company.status,
        "phone": _field_to_dict(company.contacts.get("phone", [])),
        "email": _field_to_dict(company.contacts.get("email", [])),
        "address": _field_to_dict(company.contacts.get("address", [])),
        "website": _field_to_dict(company.contacts.get("website", [])),
        "sources": company.sources,
        "stock_status": company.stock_status.value,
        "stock_status_quote": company.stock_status_quote,
        "availability": (
            {
                "status": company.availability.status.value,
                "quantity": (
                    f"{company.availability.quantity.value:g} {company.availability.quantity.unit}"
                    if company.availability.quantity
                    else None
                ),
                "evidence": company.availability.evidence,
                "source_url": company.availability.source_url,
                "checked_at": company.availability.checked_at.isoformat(),
            }
            if company.availability
            else None
        ),
        "availability_verdict": company.availability_verdict,
        "availability_verdict_text": company.availability_verdict_text,
        "price": _single_field_to_dict(company.price),
        "score": (
            {
                "relevance": round(score.relevance, 3),
                "trust": round(score.trust, 3),
                "confidence": round(score.confidence, 3),
                "total": round(score.total, 3),
            }
            if score
            else None
        ),
    }


@app.post("/api/search")
def api_search(payload: SearchRequest) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Пустой запрос")

    result = search_and_score(
        query,
        use_llm_fallback=payload.use_llm_fallback,
        deep_relevance=payload.deep_relevance,
        relevance_llm_check=payload.relevance_llm_check,
        use_trusted_suppliers=payload.use_trusted_suppliers,
        check_availability=payload.check_availability,
        probe_stepper=payload.probe_stepper,
    )
    effective_amount = result.effective_order_amount()
    summary = summarize_availability(result, effective_amount)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now()
    # timestamp с точностью до секунды + uuid-суффикс — без него два поиска
    # с похожим запросом, завершившиеся в одну и ту же секунду (несколько
    # одновременных пользователей на сервере, см. README §Деплой), получали
    # бы один и тот же filename и тихо перезатирали отчёт друг друга.
    filename = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{_slugify(query)}_{uuid.uuid4().hex[:8]}.xlsx"
    export_companies_to_excel(result, REPORTS_DIR / filename, required_qty=effective_amount, summary=summary)

    entry = {
        "filename": filename,
        "query": query,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "companies_count": len(result),
    }
    _append_report_entry(entry)

    # order_qty (штуки/комплекты) и order_length (метраж) — независимые
    # поля (см. pipeline.SearchResult), оба доступны фронтенду отдельно,
    # required_quantity — уже выбранная "эффективная" цель для отображения
    # в общей сводке (см. pipeline.effective_order_amount).
    required_quantity = (
        f"{effective_amount.value:g} {effective_amount.unit}" if effective_amount else None
    )
    return {
        "report": entry,
        "companies": [_company_to_dict(c) for c in result],
        "required_quantity": required_quantity,
        "availability_summary": summary,
    }


@app.get("/api/reports")
def api_list_reports() -> list[dict]:
    return _load_report_index()


@app.get("/api/reports/{filename}")
def api_download_report(filename: str) -> FileResponse:
    # os.path.basename режет попытки path traversal (../../etc/passwd) —
    # filename приходит из URL, то есть от пользователя.
    safe_name = os.path.basename(filename)
    path = REPORTS_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    return FileResponse(
        path,
        filename=safe_name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/categories")
def api_list_categories() -> list[dict]:
    """Полный справочник категорий (config/categories.yaml) — для
    выпадающего списка в форме ручного добавления поставщика (см.
    AddTrustedSupplierRequest): в отличие от /api/trusted-suppliers, здесь
    ВСЕ категории, включая ещё ни разу не заполненные, иначе байер не
    смог бы завести первого поставщика в свежую категорию."""
    categories_cfg = load_categories()
    return sorted(
        (
            {"code": code, "name": definition.get("name", code)}
            for code, definition in categories_cfg.items()
        ),
        key=lambda c: c["code"],
    )


@app.post("/api/trusted-suppliers")
def api_add_trusted_supplier(payload: AddTrustedSupplierRequest) -> dict:
    """Ручное добавление поставщика в конкретную категорию (см.
    AddTrustedSupplierRequest про rank/source_query для таких записей).
    Переиспользует TrustedSupplierStore.record_supplier — тот же
    upsert-путь, что и у автоматического write-back (pipeline.py), просто
    с других значений rank/source_query."""
    domain = _normalize_domain(payload.domain)
    name = payload.name.strip()
    if not domain or not name:
        raise HTTPException(status_code=400, detail="Домен и название обязательны")

    categories_cfg = load_categories()
    if payload.category_code not in categories_cfg:
        raise HTTPException(status_code=400, detail=f"Неизвестная категория {payload.category_code!r}")

    try:
        with TrustedSupplierStore() as store:
            store.record_supplier(
                domain=domain,
                name=name,
                category_code=payload.category_code,
                rank=_MANUAL_ADD_RANK,
                source_query=_MANUAL_ADD_SOURCE_QUERY,
                inn=payload.inn or None,
                phone=payload.phone or None,
                email=payload.email or None,
                address=payload.address or None,
            )
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="База поставщиков сейчас занята, попробуйте ещё раз через пару секунд"
        ) from exc
    return {"domain": domain, "category_code": payload.category_code}


@app.get("/api/trusted-suppliers")
def api_list_trusted_suppliers() -> list[dict]:
    """Просмотр БД доверенных поставщиков (trusted_suppliers.py,
    единственное персистентное хранилище проекта помимо reports/index.json)
    — заполняется автоматически при search_and_score(use_trusted_suppliers=True)
    (см. pipeline._write_back_trusted_suppliers), здесь только читается.

    Сгруппировано по категории (list_by_category, не плоский list_all) —
    под древовидный UI: одна категория может свернуться/развернуться,
    внутри — поставщики этой категории со своим (не усреднённым по всем
    категориям разом) рангом. category_name — человекочитаемое имя из
    config/categories.yaml (то же, что видит LLM при классификации, см.
    pipeline.classify_category) — сам код категории (например "F3")
    ничего не говорит байеру без него; code остаётся как fallback, если
    category_code почему-то не нашёлся в актуальном config/categories.yaml
    (например, справочник изменили, а в базе остались старые записи)."""
    categories_cfg = load_categories()
    with TrustedSupplierStore() as store:
        groups = store.list_by_category()
    for group in groups:
        group["category_name"] = categories_cfg.get(group["category_code"], {}).get(
            "name", group["category_code"]
        )
    return groups


@app.delete("/api/trusted-suppliers/{domain}")
def api_delete_trusted_supplier(domain: str) -> dict:
    """Удаляет домен целиком из БД (не одно вхождение из истории, см.
    TrustedSupplierStore.delete_supplier) — на случай, если туда попал
    маркетплейс/мусорный сайт. domain идёт в параметризованный SQL-запрос
    (не в путь к файлу), поэтому в отличие от api_download_report здесь не
    нужен os.path.basename — риска path traversal нет."""
    try:
        with TrustedSupplierStore() as store:
            deleted = store.delete_supplier(domain)
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503, detail="База поставщиков сейчас занята, попробуйте ещё раз через пару секунд"
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Поставщик с таким доменом не найден в базе")
    return {"deleted": domain}


# Статика монтируется последней: маршруты /api/* должны иметь приоритет
# над StaticFiles(html=True), который иначе перехватил бы любой путь.
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn
    from dotenv import load_dotenv

    # Подхватывает .env из корня проекта, если он есть —
    # явный `export` в шелле всё равно имеет приоритет (override=False по
    # умолчанию), .env только подставляет то, что ещё не задано. Вызывается
    # здесь, а не на уровне модуля — тесты (test_webapp.py) импортируют
    # `app` напрямую и не должны неявно подхватывать окружение разработчика.
    # Путь передаётся явно (не find_dotenv() по умолчанию) — см. cli.py про
    # то же самое решение и его мотивацию (поиск по стеку вызовов/CWD на
    # практике ненадёжен).
    load_dotenv(PROJECT_ROOT / ".env")

    logging.basicConfig(level=logging.INFO)
    # Персистентные логи на хосте (не только stdout контейнера/консоли):
    # пишем в LOGS_DIR с ротацией, чтобы не занимать диск неограниченно.
    # Каталог создаётся при первом запуске (процесс может не иметь прав на
    # корень в контейнере, но logs/ смонтирован/создан на хосте).
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = LOGS_DIR / "webapp.log"
    _rotating = RotatingFileHandler(
        _log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _rotating.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(_rotating)
    # Вариант А (design-обсуждение) — детерминированный объяснитель
    # известных ошибок, без LLM (см. log_explainer.py). Как и load_dotenv
    # выше — только здесь, не на уровне модуля: test_webapp.py импортирует
    # `app` напрямую и не должен неявно подхватывать этот хендлер.
    logging.getLogger().addHandler(ExplainingLogHandler())
    # host/port настраиваются через окружение (нужно для деплоя в
    # контейнере, см. Dockerfile/docker-compose.yml) — 127.0.0.1 по
    # умолчанию сохраняет прежнее поведение для локальной разработки вне
    # контейнера (сервер виден только с этой же машины). Внутри контейнера
    # обязателен HOST=0.0.0.0 — иначе проброшенный порт всё равно никуда
    # не ведёт: 127.0.0.1 внутри контейнера означает "только из этого же
    # контейнера", а не "с хоста через docker -p".
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
