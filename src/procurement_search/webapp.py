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
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from procurement_search.export import export_companies_to_excel
from procurement_search.models import Company, FieldValue
from procurement_search.pipeline import search_and_score

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_ROOT / "reports"
INDEX_PATH = REPORTS_DIR / "index.json"
STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI(title="Procurement Search")


class SearchRequest(BaseModel):
    query: str
    use_llm_fallback: bool = False
    deep_relevance: bool = False
    relevance_llm_check: bool = False


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w]+", "_", text.strip().lower(), flags=re.UNICODE).strip("_")
    return slug[:max_len] or "query"


def _load_report_index() -> list[dict]:
    if not INDEX_PATH.exists():
        return []
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def _save_report_index(entries: list[dict]) -> None:
    INDEX_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _field_to_dict(field_values: list[FieldValue]) -> dict | None:
    if not field_values:
        return None
    fv = field_values[0]
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

    companies = search_and_score(
        query,
        use_llm_fallback=payload.use_llm_fallback,
        deep_relevance=payload.deep_relevance,
        relevance_llm_check=payload.relevance_llm_check,
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now()
    filename = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{_slugify(query)}.xlsx"
    export_companies_to_excel(companies, REPORTS_DIR / filename)

    entry = {
        "filename": filename,
        "query": query,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "companies_count": len(companies),
    }
    entries = _load_report_index()
    entries.insert(0, entry)
    _save_report_index(entries)

    return {"report": entry, "companies": [_company_to_dict(c) for c in companies]}


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


# Статика монтируется последней: маршруты /api/* должны иметь приоритет
# над StaticFiles(html=True), который иначе перехватил бы любой путь.
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn
    from dotenv import load_dotenv

    # Подхватывает .env из корня проекта (см. .env.example), если он есть —
    # явный `export` в шелле всё равно имеет приоритет (override=False по
    # умолчанию), .env только подставляет то, что ещё не задано. Вызывается
    # здесь, а не на уровне модуля — тесты (test_webapp.py) импортируют
    # `app` напрямую и не должны неявно подхватывать окружение разработчика.
    # Путь передаётся явно (не find_dotenv() по умолчанию) — см. cli.py про
    # то же самое решение и его мотивацию (поиск по стеку вызовов/CWD на
    # практике ненадёжен).
    load_dotenv(PROJECT_ROOT / ".env")

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
