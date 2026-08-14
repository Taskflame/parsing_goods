"""DuckDuckGo — общий веб-поиск как канал генерации кандидатов
(design_doc §3, шаг [2]: "веб-поиск (общий)" был запланирован, но не
реализован до этого момента; pulscen.ru/optlist.ru — специализированные
обязательные источники, этот — расширение охвата на весь интернет).

В отличие от pulscen.ru/optlist.ru (карточки компаний с явными полями
телефон/email), обычная поисковая выдача даёт только заголовок, ссылку и
сниппет. Здесь это принимается осознанно: часть контактных полей у таких
кандидатов будет пустой — честнее, чем выдумывать данные (design_doc §1).
Слабая точность recall-канала компенсируется на следующих шагах:
дедупликацией с pulscen/optlist (если это та же компания — телефон/домен
совпадут) и резолвингом в ИНН через enrichment.DadataEnricher, который
отсекает случайные нерелевантные страницы (новости, форумы, агрегаторы
без карточки конкретного юрлица) на этапе, когда они не резолвятся ни в
одну организацию.

Используется HTML-версия DuckDuckGo (html.duckduckgo.com) — не требует
JS и API-ключа. Как и для остальных источников, соблюдаются robots.txt и
пауза между запросами (design_doc §7, п.4-5) — общей инфраструктурой из
sources/base.py (fetch_url), без дублирования.
"""

from __future__ import annotations

import logging
from datetime import date
from urllib.parse import parse_qs, quote, unquote, urlparse

from bs4 import BeautifulSoup

from procurement_search.models import Candidate
from procurement_search.sources.base import EMAIL_RE, PHONE_RE, SourceConfig, fetch_url, first_match

logger = logging.getLogger(__name__)


def _extract_real_url(href: str | None) -> str | None:
    """DuckDuckGo HTML отдаёт ссылки через свой редиректор
    (//duckduckgo.com/l/?uddg=<urlencoded-адрес>&...), а не прямой URL —
    настоящий адрес закодирован в параметре uddg."""
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    if "uddg" in qs and qs["uddg"]:
        return unquote(qs["uddg"][0])
    return href if href.startswith("http") else None


class DuckDuckGoSource:
    def __init__(self, config: SourceConfig):
        self.config = config

    def build_search_url(self, query: str) -> str:
        return self.config.search_url_template.format(query=quote(query))

    def search(self, query: str) -> list[Candidate]:
        url = self.build_search_url(query)
        html = fetch_url(
            url,
            user_agent=self.config.user_agent,
            timeout=self.config.timeout_seconds,
            delay=self.config.request_delay_seconds,
            respect_robots=self.config.respect_robots_txt,
        )
        if html is None:
            return []
        return self.parse_search_html(html)

    def parse_search_html(self, html: str) -> list[Candidate]:
        soup = BeautifulSoup(html, "lxml")
        candidates: list[Candidate] = []

        for result in soup.select("div.result"):
            title_el = result.select_one("a.result__a")
            if title_el is None:
                continue
            name = title_el.get_text(strip=True)
            if not name:
                continue

            real_url = _extract_real_url(title_el.get("href"))
            if real_url is None:
                continue

            snippet_el = result.select_one("a.result__snippet, div.result__snippet")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else None

            candidates.append(
                Candidate(
                    source=self.config.name,
                    source_url=real_url,
                    name_raw=name,
                    phone_raw=first_match(PHONE_RE, snippet),
                    email_raw=first_match(EMAIL_RE, snippet),
                    description_raw=snippet,
                    # website = real_url, а не источник листинга (в отличие от
                    # CSS-fallback у каталогов) — ссылка из поиска УЖЕ ведёт на
                    # сайт самой компании, а не на профиль внутри площадки.
                    website=real_url,
                    scraped_at=date.today(),
                )
            )
            if len(candidates) >= self.config.max_results_per_query:
                break

        return candidates


def build_default(config_dict: dict) -> DuckDuckGoSource:
    return DuckDuckGoSource(SourceConfig.from_dict("duckduckgo", config_dict["duckduckgo"]))