"""Общая инфраструктура для источников-каталогов (pulscen.ru, optlist.ru, ...).

Стратегия извлечения (design_doc §7), в порядке приоритета:
  1. schema.org JSON-LD / microdata — устойчиво к рестайлу вёрстки.
  2. CSS-селекторы из config/sources.yaml — fallback, требует калибровки.
  3. Regex по телефону/email на сыром тексте карточки — последняя страховка.

Все конкретные источники (PulscenSource, OptlistSource) наследуются от
CatalogSource и переопределяют только `build_search_url` — вся остальная
логика (rate limiting, robots.txt, JSON-LD/CSS-парсинг) общая.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from procurement_search.models import Candidate

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"(?:\+7|8)[\s\-\(]?\d{3}[\)\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Эвристика, не грамматика: у российских адресов нет единого формата,
# поэтому шаблон требует минимум "тип улицы + номер дома" как надёжный
# якорь (город/индекс — опциональны, но одни они слишком похожи на
# случайный текст, чтобы матчиться без якоря дальше по строке).
ADDRESS_RE = re.compile(
    r"(?:\d{6},?\s*)?"
    r"(?:г\.?\s*[А-ЯЁ][а-яё\-]+,?\s*)?"
    r"(?:ул\.?|улица|пр-?кт|проспект|пер\.?|переулок|ш\.?|шоссе|наб\.?|набережная)\s*"
    r"[А-ЯЁа-яё0-9\-\s]{2,40}?,?\s*"
    r"(?:д\.?|дом)\s*\d+[а-яёА-ЯЁ]?(?:\s*,?\s*(?:оф\.?|офис|кв\.?|корп\.?)\s*\d+)?",
    re.IGNORECASE,
)

# schema.org типы, из которых нас интересуют факты о поставщике.
RELEVANT_JSONLD_TYPES = {"Organization", "LocalBusiness", "Product", "Store", "Corporation"}


class RobotsChecker:
    """Кеширующая проверка robots.txt по домену (design_doc §7, п.4)."""

    def __init__(self) -> None:
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}
        # Домены, для которых robots.txt не удалось прочитать — храним отдельно
        # от _parsers, потому что непрочитанный RobotFileParser (last_checked
        # не выставлен) сам по себе даёт can_fetch()=False, а не True. Раньше
        # такой недочитанный парсер всё равно клался в _parsers "чтобы не
        # блокировать", но реально блокировал уже со второго вызова для того
        # же домена — is_allowed() не доходил до can_fetch() только на самом
        # первом обращении, а дальше шёл по кешу и получал False.
        self._unreadable: set[str] = set()

    def is_allowed(self, url: str, user_agent: str) -> bool:
        domain = urlparse(url).netloc
        if domain in self._unreadable:
            return True
        if domain not in self._parsers:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{urlparse(url).scheme}://{domain}/robots.txt"
            try:
                rp.set_url(robots_url)
                rp.read()
            except Exception:
                logger.warning("Не удалось прочитать %s, продолжаем осторожно", robots_url)
                # Если robots.txt недоступен — не блокируем, но это осознанный
                # компромисс для прототипа; в проде уместнее fail-closed.
                self._unreadable.add(domain)
                return True
            self._parsers[domain] = rp
        return self._parsers[domain].can_fetch(user_agent, url)


_ROBOTS_CHECKER = RobotsChecker()

_session = requests.Session()


def fetch_url(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    delay: float,
    respect_robots: bool,
    session: requests.Session | None = None,
) -> str | None:
    """Общая HTTP-логика для всех источников: robots.txt, таймаут, пауза
    между запросами, единообразное логирование ошибок. Вынесена из
    CatalogSource._fetch, чтобы источники, не вписывающиеся в модель
    "каталог с карточками" (например, sources/duckduckgo.py — обычный
    веб-поиск), не дублировали её."""
    if respect_robots and not _ROBOTS_CHECKER.is_allowed(url, user_agent):
        logger.warning("robots.txt запрещает доступ к %s — пропускаем", url)
        return None
    session = session or _session
    try:
        resp = session.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Ошибка запроса к %s: %s", url, exc)
        return None
    finally:
        time.sleep(delay)
    return resp.text


@dataclass
class SourceConfig:
    name: str
    base_url: str
    search_url_template: str
    request_delay_seconds: float
    timeout_seconds: float
    user_agent: str
    respect_robots_txt: bool
    max_results_per_query: int
    selectors: dict[str, str]

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "SourceConfig":
        return cls(
            name=name,
            base_url=d["base_url"],
            search_url_template=d["search_url_template"],
            request_delay_seconds=float(d.get("request_delay_seconds", 2.0)),
            timeout_seconds=float(d.get("timeout_seconds", 15.0)),
            user_agent=d.get("user_agent", "ProcurementSearchBot/0.1"),
            respect_robots_txt=bool(d.get("respect_robots_txt", True)),
            max_results_per_query=int(d.get("max_results_per_query", 30)),
            selectors=d.get("selectors", {}),
        )


def extract_jsonld(soup: BeautifulSoup) -> list[dict]:
    """Возвращает список schema.org объектов релевантных типов из <script type="application/ld+json">."""
    results: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            candidates = graph if isinstance(graph, list) else [item]
            for c in candidates:
                if isinstance(c, dict) and c.get("@type") in RELEVANT_JSONLD_TYPES:
                    results.append(c)
    return results


def _select_one_safe(item, selector: str | None):
    """item.select_one, но не падает на пустом/незаполненном селекторе —
    незаполненный `*_selector` в конфиге (селектор ещё не откалиброван,
    см. config/sources.yaml) должен просто ничего не находить, а не рушить
    парсинг всей страницы."""
    if not selector:
        return None
    try:
        return item.select_one(selector)
    except Exception:
        logger.warning("Некорректный CSS-селектор %r в config/sources.yaml", selector)
        return None


def _text_or_none(el) -> str | None:
    if el is None:
        return None
    text = el.get_text(strip=True)
    return text or None


def first_match(pattern: re.Pattern, text: str | None) -> str | None:
    if not text:
        return None
    m = pattern.search(text)
    return m.group(0) if m else None


class CatalogSource(ABC):
    """Базовый класс источника-каталога. Конкретные источники переопределяют
    только `build_search_url`; остальное общее."""

    def __init__(self, config: SourceConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    @abstractmethod
    def build_search_url(self, query: str) -> str:
        ...

    def _fetch(self, url: str) -> str | None:
        return fetch_url(
            url,
            user_agent=self.config.user_agent,
            timeout=self.config.timeout_seconds,
            delay=self.config.request_delay_seconds,
            respect_robots=self.config.respect_robots_txt,
            session=self.session,
        )

    def search(self, query: str) -> list[Candidate]:
        url = self.build_search_url(query)
        html = self._fetch(url)
        if html is None:
            return []
        return self.parse_search_html(html, source_url=url)

    def parse_search_html(self, html: str, source_url: str) -> list[Candidate]:
        soup = BeautifulSoup(html, "lxml")

        jsonld_items = extract_jsonld(soup)
        if jsonld_items:
            return [
                c
                for c in (self._candidate_from_jsonld(item, source_url) for item in jsonld_items)
                if c is not None
            ][: self.config.max_results_per_query]

        return self._parse_with_css_fallback(soup, source_url)

    def _candidate_from_jsonld(self, item: dict, source_url: str) -> Candidate | None:
        name = item.get("name")
        if not name:
            return None
        address = item.get("address")
        if isinstance(address, dict):
            address = ", ".join(
                str(v) for v in address.values() if isinstance(v, str) and v
            )
        elif not isinstance(address, str):
            address = None

        return Candidate(
            source=self.config.name,
            source_url=source_url,
            name_raw=str(name),
            phone_raw=item.get("telephone"),
            email_raw=item.get("email"),
            address_raw=address,
            description_raw=item.get("description"),
            website=item.get("url"),
            scraped_at=date.today(),
        )

    def _parse_with_css_fallback(self, soup: BeautifulSoup, source_url: str) -> list[Candidate]:
        sel = self.config.selectors
        item_selector = sel.get("item_selector")
        if not item_selector:
            return []

        candidates: list[Candidate] = []
        for item in soup.select(item_selector)[: self.config.max_results_per_query]:
            name_el = _select_one_safe(item, sel.get("name_selector"))
            name = _text_or_none(name_el)
            if not name:
                continue

            link_el = _select_one_safe(item, sel.get("link_selector"))
            href = link_el.get("href") if link_el else None
            # item_url — профиль компании ВНУТРИ каталога (pulscen.ru/optlist.ru),
            # это НЕ сайт компании. Используется только для source_url/провенанса.
            item_url = urljoin(self.config.base_url, href) if href else source_url

            # website — опциональный отдельный селектор на исходящую ссылку на
            # собственный сайт компании (если каталог её показывает отдельно).
            # Без него дедуп по домену просто не сработает для этого источника,
            # что безопаснее, чем ложно принять домен каталога за домен компании.
            website_el = _select_one_safe(item, sel.get("website_selector"))
            website = website_el.get("href") if website_el else None

            item_text = item.get_text(" ", strip=True)
            address_text = _text_or_none(_select_one_safe(item, sel.get("address_selector")))
            description_text = _text_or_none(
                _select_one_safe(item, sel.get("description_selector"))
            )
            phone_text = _text_or_none(_select_one_safe(item, sel.get("phone_selector")))

            candidates.append(
                Candidate(
                    source=self.config.name,
                    source_url=item_url,
                    name_raw=name,
                    phone_raw=phone_text or first_match(PHONE_RE, item_text),
                    email_raw=first_match(EMAIL_RE, item_text),
                    address_raw=address_text,
                    description_raw=description_text,
                    website=website,
                    scraped_at=date.today(),
                )
            )
        return candidates
