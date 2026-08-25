"""Google Custom Search JSON API — источник "поиск по всему интернету"
(ТЗ п.4: "работать по зарубежным источникам (в т.ч. Китай, агрегаторы
типа Alibaba)" + design_doc §3, шаг [2], канал "весь интернет").

В отличие от pulscen.ru/optlist.ru/duckduckgo (HTML-скрапинг публичных
страниц), это официальный API с ключом — намеренный выбор после того, как
скрапинг обычной поисковой выдачи (html.duckduckgo.com) на практике упёрся
в капчу "select all squares containing a duck": крупные поисковики
защищают HTML-выдачу от автоматических клиентов, и обходить эту защиту —
не задача парсера поставщиков. Google CSE тем и хорош, что это официальный
программный интерфейс, а не то, что владелец сайта пытается заблокировать.

Бесплатный тариф — 100 запросов/день без привязки карты. Настройка:
  1. Google Cloud API-ключ: console.cloud.google.com -> APIs & Services ->
     Credentials -> Create API key (включить Custom Search API).
  2. Programmable Search Engine с опцией "Search the entire web":
     programmablesearchengine.google.com -> создать -> Search engine ID.

Обе переменные окружения — GOOGLE_CSE_API_KEY и GOOGLE_CSE_CX. Как и
DADATA_API_KEY/YANDEX_FM_API_KEY в остальном проекте, источник включается
наличием переменных в окружении, без правки кода (см.
pipeline._default_enricher про тот же паттерн).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date

import requests

from procurement_search.models import Candidate
from procurement_search.sources.base import EMAIL_RE, PHONE_RE, SUPPLIER_QUERY_SUFFIX, first_match

logger = logging.getLogger(__name__)

GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"
# Google CSE отдаёт максимум 10 результатов за один запрос (параметр num).
MAX_RESULTS_PER_REQUEST = 10


class GoogleCseSource:
    """Источник "поиск по всему интернету" через Google Custom Search JSON API.

    Отдаёт только заголовок/ссылку/сниппет — без отдельных полей телефон/
    email от API, извлекаем регексом из сниппета. Часть контактных полей у
    таких кандидатов останется пустой — это честно отражает то, что
    источник даёт (design_doc §1), а не выдумка.
    """

    def __init__(
        self,
        api_key: str,
        cx: str,
        name: str = "google_cse",
        timeout: float = 15.0,
        request_delay_seconds: float = 1.0,
        max_results_per_query: int = MAX_RESULTS_PER_REQUEST,
        enrich_query: bool = True,
        session: requests.Session | None = None,
    ):
        if not api_key or not cx:
            raise ValueError(
                "GoogleCseSource требует api_key и cx — см. GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX"
            )
        self.api_key = api_key
        self.cx = cx
        self.name = name
        self.timeout = timeout
        self.request_delay_seconds = request_delay_seconds
        self.max_results_per_query = min(max_results_per_query, MAX_RESULTS_PER_REQUEST)
        self.enrich_query = enrich_query
        self.session = session or requests.Session()

    def _build_query_text(self, query: str) -> str:
        return query + SUPPLIER_QUERY_SUFFIX if self.enrich_query else query

    def search(self, query: str) -> list[Candidate]:
        try:
            resp = self.session.get(
                GOOGLE_CSE_URL,
                params={
                    "key": self.api_key,
                    "cx": self.cx,
                    "q": self._build_query_text(query),
                    "num": self.max_results_per_query,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException:
            logger.warning("Google CSE запрос не удался для %r", query, exc_info=True)
            return []
        finally:
            time.sleep(self.request_delay_seconds)

        items = resp.json().get("items") or []
        return [c for c in (self._candidate_from_item(item) for item in items) if c is not None]

    def _candidate_from_item(self, item: dict) -> Candidate | None:
        title = item.get("title")
        link = item.get("link")
        if not title or not link:
            return None
        snippet = item.get("snippet")
        return Candidate(
            source=self.name,
            source_url=link,
            name_raw=title,
            phone_raw=first_match(PHONE_RE, snippet),
            email_raw=first_match(EMAIL_RE, snippet),
            description_raw=snippet,
            # ссылка из выдачи ведёт на саму страницу (сайт компании,
            # карточку на Alibaba и т.п.), а не на профиль внутри
            # стороннего каталога — как и у duckduckgo.py, это годится
            # для дедупа по домену.
            website=link,
            scraped_at=date.today(),
        )


def build_default(config_dict: dict) -> GoogleCseSource | None:
    """None, если GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX не заданы в окружении —
    источник тогда просто отсутствует в пайплайне, без ошибки (см. модуль
    pipeline.py, где вызывающий код обязан обработать None)."""
    api_key = os.environ.get("GOOGLE_CSE_API_KEY")
    cx = os.environ.get("GOOGLE_CSE_CX")
    if not api_key or not cx:
        return None

    cfg = config_dict.get("google_cse", {})
    return GoogleCseSource(
        api_key=api_key,
        cx=cx,
        timeout=float(cfg.get("timeout_seconds", 15.0)),
        request_delay_seconds=float(cfg.get("request_delay_seconds", 1.0)),
        max_results_per_query=int(cfg.get("max_results_per_query", MAX_RESULTS_PER_REQUEST)),
        enrich_query=bool(cfg.get("enrich_query", True)),
    )
