"""Yandex Search API (Yandex Cloud) — источник "поиск по всему интернету"
для RU-рынка (ТЗ п.4 "глобальный поиск" + design_doc §3, шаг [2]).

Выбран после того, как Google Custom Search JSON API на практике упёрся в
требование привязать карту для верификации биллинга — карту международной
платёжной системы к российскому Google-аккаунту привязать не вышло. Yandex
Search API платный (свободного тарифа сейчас нет — старый бесплатный
Yandex.XML закрыт), но принимает российские карты и по смыслу лучше
подходит под требование ТЗ "работать по российскому рынку".

ВАЖНО про качество этой реализации: в отличие от google_cse.py (который
проверен вживую до упора в биллинг), этот модуль написан ПО ДОКУМЕНТАЦИИ
Yandex Cloud Search API, без доступа к реальному ключу — см. design_doc
§11 про тот же осознанный компромисс с CSS-селекторами pulscen/optlist.
Формат запроса/ответа документирован Yandex как нестабильный внутри одной
major-версии (v2), но конкретные названия полей стоит перепроверить на
первом реальном ответе и поправить `_parse_xml`, если что-то разойдётся.

Особенности API, которые важны для реализации:
  - Аутентификация: заголовок `Authorization: Api-Key <ключ>`.
  - Запрос асинхронный: POST на .../searchAsync возвращает operation id,
    результат нужно забирать отдельным поллингом GET-а до done=true.
  - Тело успешного ответа — НЕ JSON с поставщиками, а поле `rawData`:
    результат в формате XML (аналог старого Yandex.XML), закодированный
    в base64 — раскодировать и распарсить отдельно.
  - Обязателен `folderId` (идентификатор каталога в Yandex Cloud) —
    отдельная сущность от самого API-ключа.
"""

from __future__ import annotations

import base64
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import date

import requests

from procurement_search.models import Candidate
from procurement_search.sources.base import EMAIL_RE, PHONE_RE, SUPPLIER_QUERY_SUFFIX, first_match

logger = logging.getLogger(__name__)

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_URL_TEMPLATE = "https://operation.api.cloud.yandex.net/operations/{operation_id}"
DEFAULT_MAX_RESULTS = 10


class YandexSearchSource:
    """Источник "поиск по всему интернету" через Yandex Search API.

    Как и GoogleCseSource, отдаёт заголовок/ссылку/сниппет без выделенных
    полей телефон/email — извлекаем регексом из сниппета (описание того же
    честного компромисса — см. google_cse.py)."""

    def __init__(
        self,
        api_key: str,
        folder_id: str,
        name: str = "yandex_search",
        timeout: float = 15.0,
        request_delay_seconds: float = 1.0,
        max_results_per_query: int = DEFAULT_MAX_RESULTS,
        poll_interval_seconds: float = 1.0,
        poll_timeout_seconds: float = 20.0,
        enrich_query: bool = True,
        session: requests.Session | None = None,
    ):
        if not api_key or not folder_id:
            raise ValueError(
                "YandexSearchSource требует api_key и folder_id — см. "
                "YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID"
            )
        self.api_key = api_key
        self.folder_id = folder_id
        self.name = name
        self.timeout = timeout
        self.request_delay_seconds = request_delay_seconds
        self.max_results_per_query = max_results_per_query
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self.enrich_query = enrich_query
        self.session = session or requests.Session()

    def _headers(self) -> dict:
        return {"Authorization": f"Api-Key {self.api_key}"}

    def _build_query_text(self, query: str) -> str:
        return query + SUPPLIER_QUERY_SUFFIX if self.enrich_query else query

    def search(self, query: str) -> list[Candidate]:
        try:
            resp = self.session.post(
                SEARCH_URL,
                json={
                    "query": {
                        "searchType": "SEARCH_TYPE_RU",
                        "queryText": self._build_query_text(query),
                        "page": "0",
                    },
                    "folderId": self.folder_id,
                    "responseFormat": "FORMAT_XML",
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException:
            logger.warning("Yandex Search API запрос не удался для %r", query, exc_info=True)
            return []
        finally:
            time.sleep(self.request_delay_seconds)

        operation_id = resp.json().get("id")
        if not operation_id:
            logger.warning("Yandex Search API не вернул operation id для %r", query)
            return []

        raw_xml = self._poll_operation(operation_id)
        if raw_xml is None:
            return []
        return self._parse_xml(raw_xml)

    def _poll_operation(self, operation_id: str) -> str | None:
        deadline = time.monotonic() + self.poll_timeout_seconds
        while time.monotonic() < deadline:
            try:
                resp = self.session.get(
                    OPERATION_URL_TEMPLATE.format(operation_id=operation_id),
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                resp.raise_for_status()
            except requests.RequestException:
                logger.warning("Не удалось опросить операцию Yandex Search API %s", operation_id, exc_info=True)
                return None

            data = resp.json()
            if data.get("done"):
                if "error" in data:
                    logger.warning("Yandex Search API вернул ошибку операции: %s", data["error"])
                    return None
                raw_data_b64 = (data.get("response") or {}).get("rawData")
                if not raw_data_b64:
                    return None
                return base64.b64decode(raw_data_b64).decode("utf-8", errors="replace")
            time.sleep(self.poll_interval_seconds)

        logger.warning(
            "Операция Yandex Search API %s не завершилась за %.0fс", operation_id, self.poll_timeout_seconds
        )
        return None

    def _parse_xml(self, xml_text: str) -> list[Candidate]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            logger.warning("Не удалось распарсить XML-ответ Yandex Search API")
            return []

        candidates: list[Candidate] = []
        for doc in root.iter("doc"):
            url_el = doc.find("url")
            if url_el is None or not (url_el.text or "").strip():
                continue
            url = url_el.text.strip()

            title_el = doc.find("title")
            title = "".join(title_el.itertext()).strip() if title_el is not None else url
            if not title:
                title = url

            passage_el = doc.find(".//passage")
            snippet = "".join(passage_el.itertext()).strip() if passage_el is not None else None

            candidates.append(
                Candidate(
                    source=self.name,
                    source_url=url,
                    name_raw=title,
                    phone_raw=first_match(PHONE_RE, snippet),
                    email_raw=first_match(EMAIL_RE, snippet),
                    description_raw=snippet,
                    website=url,
                    scraped_at=date.today(),
                )
            )
            if len(candidates) >= self.max_results_per_query:
                break
        return candidates


def build_default(config_dict: dict) -> YandexSearchSource | None:
    """None, если YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID не заданы — как и
    GoogleCseSource, источник включается переменными окружения без правки
    кода (см. pipeline.py)."""
    api_key = os.environ.get("YANDEX_SEARCH_API_KEY")
    folder_id = os.environ.get("YANDEX_FOLDER_ID")
    if not api_key or not folder_id:
        return None

    cfg = config_dict.get("yandex_search", {})
    return YandexSearchSource(
        api_key=api_key,
        folder_id=folder_id,
        timeout=float(cfg.get("timeout_seconds", 15.0)),
        request_delay_seconds=float(cfg.get("request_delay_seconds", 1.0)),
        max_results_per_query=int(cfg.get("max_results_per_query", DEFAULT_MAX_RESULTS)),
        poll_interval_seconds=float(cfg.get("poll_interval_seconds", 1.0)),
        poll_timeout_seconds=float(cfg.get("poll_timeout_seconds", 20.0)),
        enrich_query=bool(cfg.get("enrich_query", True)),
    )
