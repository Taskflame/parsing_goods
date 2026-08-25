"""Yandex Search API — генеративный ответ ("gen search", design-обсуждение
про "yandex_gen_search"). Тот же продукт/ключ, что и sources/yandex_search.py
(тот же YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID) — не отдельный сервис, а
третий режим ответа: вместо списка сырых документов LLM Яндекса синтезирует
один текстовый ответ + список процитированных источников (sources[]).

Эндпоинт `/v2/gen/search` не описан в официальной документации доступным
для автоматического чтения образом (страницы aistudio.yandex.ru/
yandex.cloud блокируются капчей) — определён по официальному .proto
Яндекса (github.com/yandex-cloud/cloudapi, gen_search_service.proto) и
ПОДТВЕРЖДЁН вживую на реальном ключе (design-обсуждение). В отличие от
classic /v2/web/searchAsync — синхронный, без polling: ответ приходит
сразу, одним JSON-массивом из одного объекта (при getPartialResults=false,
который здесь не переопределяется — стриминг NDJSON не понадобился).

Почему source_url/website берутся только из sources[].used=true (по
умолчанию, only_used_sources=True): проверено вживую — для "мотоцикл
кроссовый IRBIS 250 кубов" Yandex вернул в sources[] и avito.ru, и
pulscen.ru (те самые B2C-площадки, из-за которых заведена деприоритизация
в pipeline.py/scoring.is_marketplace_domain), но LLM сама их НЕ
процитировала в ответе (used=false) — предпочла прямые сайты дилеров.
used=true — более сильный сигнал релевантности, чем что-либо
детерминированное, что у нас есть для этого источника.

ПЛАТНО и ЗАМЕТНО ДОРОЖЕ классического yandex_search.py (~5₽ за 1000
синхронных запросов по прайсингу на момент проверки — не отслеживается
автоматически, см. README) — поэтому источник НЕ включается автоматически
вместе с YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID, а требует отдельного
явного флага YANDEX_GEN_SEARCH_ENABLED=true (design_doc-стиль решения:
дорогой источник — только по явному opt-in, как LLM-fallback в
query_normalizer.py).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date

import requests

from procurement_search.models import Candidate

logger = logging.getLogger(__name__)

GEN_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/gen/search"
DEFAULT_MAX_RESULTS = 10


class YandexGenSearchSource:
    """Источник "поиск по всему интернету" через генеративный ответ Yandex
    Search API — в Candidate попадают только процитированные LLM
    источники (см. докстринг модуля про only_used_sources).

    В отличие от YandexSearchSource, здесь нет отдельных полей
    телефон/email/сниппет в самих sources[] — Candidate.description_raw
    остаётся None (честнее, чем выдумывать сниппет из общего текста
    ответа, где несколько источников перемешаны, design_doc §1)."""

    def __init__(
        self,
        api_key: str,
        folder_id: str,
        name: str = "yandex_gen_search",
        timeout: float = 30.0,
        request_delay_seconds: float = 1.0,
        max_results_per_query: int = DEFAULT_MAX_RESULTS,
        only_used_sources: bool = True,
        session: requests.Session | None = None,
    ):
        if not api_key or not folder_id:
            raise ValueError(
                "YandexGenSearchSource требует api_key и folder_id — см. "
                "YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID"
            )
        self.api_key = api_key
        self.folder_id = folder_id
        self.name = name
        self.timeout = timeout
        self.request_delay_seconds = request_delay_seconds
        self.max_results_per_query = max_results_per_query
        self.only_used_sources = only_used_sources
        self.session = session or requests.Session()

    def _headers(self) -> dict:
        return {"Authorization": f"Api-Key {self.api_key}"}

    def search(self, query: str) -> list[Candidate]:
        try:
            resp = self.session.post(
                GEN_SEARCH_URL,
                json={
                    "messages": [{"role": "ROLE_USER", "content": query}],
                    "folderId": self.folder_id,
                    "searchType": "SEARCH_TYPE_RU",
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException:
            logger.warning("Yandex gen-search запрос не удался для %r", query, exc_info=True)
            return []
        finally:
            time.sleep(self.request_delay_seconds)

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("Yandex gen-search вернул не-JSON ответ для %r", query)
            return []

        # getPartialResults=false (по умолчанию, не переопределяется) ->
        # массив из ровно одного GenSearchResponse, а не NDJSON-стрим.
        if not payload:
            return []
        sources = payload[0].get("sources") or []

        candidates: list[Candidate] = []
        for src in sources:
            if self.only_used_sources and not src.get("used"):
                continue
            url = src.get("url")
            title = src.get("title")
            if not url or not title:
                continue
            candidates.append(
                Candidate(
                    source=self.name,
                    source_url=url,
                    name_raw=title,
                    website=url,
                    scraped_at=date.today(),
                )
            )
            if len(candidates) >= self.max_results_per_query:
                break
        return candidates


def build_default(config_dict: dict) -> YandexGenSearchSource | None:
    """None, если YANDEX_GEN_SEARCH_ENABLED не выставлен в true ИЛИ не
    заданы YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID (общие с yandex_search) —
    как и остальные опциональные источники, включается переменными
    окружения без правки кода, но здесь два условия, а не одно, из-за
    заметно более высокой цены запроса (см. докстринг модуля)."""
    if os.environ.get("YANDEX_GEN_SEARCH_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return None
    api_key = os.environ.get("YANDEX_SEARCH_API_KEY")
    folder_id = os.environ.get("YANDEX_FOLDER_ID")
    if not api_key or not folder_id:
        return None

    cfg = config_dict.get("yandex_gen_search", {})
    return YandexGenSearchSource(
        api_key=api_key,
        folder_id=folder_id,
        timeout=float(cfg.get("timeout_seconds", 30.0)),
        request_delay_seconds=float(cfg.get("request_delay_seconds", 1.0)),
        max_results_per_query=int(cfg.get("max_results_per_query", DEFAULT_MAX_RESULTS)),
        only_used_sources=bool(cfg.get("only_used_sources", True)),
    )
