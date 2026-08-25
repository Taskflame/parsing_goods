"""Общая HTTP-инфраструктура для источников-каталогов (design_doc §7):
robots.txt, таймаут, пауза между запросами, единообразное логирование
ошибок — используется краулингом сайтов кандидатов (site_relevance.py) и
источниками общего веб-поиска (google_cse.py, yandex_search.py,
yandex_gen_search.py).

CatalogSource/SourceConfig (JSON-LD/CSS-парсинг каталогов) убраны вместе с
pulscen.py/optlist.py (design-обсуждение: CSS-селекторы в config/sources.yaml
так и остались неоткалиброванными PLACEHOLDER'ами с самого начала — 0
кандидатов) — этот механизм был нужен только им.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


# Скобка вокруг кода — свой собственный необязательный элемент, а не один
# символ-разделитель наравне с пробелом/дефисом: "+7 (495) 256-16-36"
# (пробел И скобка подряд, частый формат) не матчился старой версией, где
# между "+7"/"8" и кодом допускался только ОДИН разделитель (design-
# обсуждение: проверено вживую на реальном сайте, рабочий номер не находился).
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Эвристика, не грамматика: у российских адресов нет единого формата,
# поэтому шаблон требует минимум "тип улицы + номер дома" как надёжный
# якорь (город/индекс — опциональны, но одни они слишком похожи на
# случайный текст, чтобы матчиться без якоря дальше по строке).
#
# Два независимых варианта порядка слов ("Кутузовский проспект" — название
# ПЕРЕД типом улицы, а не после, как у "ул. Полковая"). "дом"/"д." перед
# номером необязателен — многие реальные адреса пишут номер сразу после
# названия улицы, без явного маркера.
#
# У КОРОТКИХ сокращений (ул/ш/пер/наб/пр-кт/б-р) точка после них теперь
# ОБЯЗАТЕЛЬНА, а у полных слов (улица/шоссе/...) — граница слова \b
# (design-обсуждение: без этого "рег[ул]ятор" и "[ш]т" матчились как
# начало адреса — короткое сокращение без якоря совпадало с произвольными
# буквами ВНУТРИ других слов, а после того как "дом"/"д." перед номером
# стал необязателен, дальше по строке хватало любых цифр в пределах 40
# символов, чтобы "срастить" в фальшивый адрес — реальный баг, найденный
# на живой выдаче).
ADDRESS_RE = re.compile(
    r"(?:\d{6},?\s*)?"
    r"(?:г\.?\s*[А-ЯЁ][а-яё\-]+,?\s*)?"
    r"(?:"
    r"(?:\bул\.\s*|\bулица\b\s*|\bпр-?кт\.\s*|\bпросп\.\s*|\bпроспект\b\s*|\bпер\.\s*|"
    r"\bпереулок\b\s*|\bб-?р\.\s*|\bбульвар\b\s*|\bш\.\s*|\bшоссе\b\s*|\bнаб\.\s*|\bнабережная\b\s*)"
    r"[А-ЯЁа-яё0-9\-\s]{2,40}?"
    r"|"
    r"\b[А-ЯЁ][а-яё\-]{2,30}\s+(?:проспект|переулок|шоссе|набережная|бульвар)\b"
    r")"
    r",?\s*"
    r"(?:(?:д\.?|дом)\s*)?\d+[а-яёА-ЯЁ]?(?:\s*,?\s*(?:оф\.?|офис|кв\.?|корп\.?)\s*\d+)?",
    re.IGNORECASE,
)

# B2B-обогащение запроса для источников общего веб-поиска (Yandex Search,
# Google CSE) — общее место, чтобы не дублировать строку в каждом источнике
# по отдельности. Проверено вживую на Yandex Search API: без этого суффикса
# "генератор бензиновый 5 квт" отдавал Ozon/Wildberries/DNS-shop (розничные
# категорийные страницы), с ним — patriot-opt.ru, tss-sklad.ru, официальные
# сайты производителей (A-IPOWER, TSS). Оператор "|" (OR) поддерживается
# синтаксисом запросов Yandex Search API и реально меняет ранжирование, а
# не просто добавляет слова в текст запроса — для Google CSE это обычные
# слова запроса (синтаксис "|" для OR не документирован так же явно), но
# даже как обычные слова они смещают выдачу в сторону B2B-страниц, а не
# потребительских маркетплейсов.
SUPPLIER_QUERY_SUFFIX = " (поставщик | производитель | оптом)"


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
    """Общая HTTP-логика: robots.txt, таймаут, пауза между запросами,
    единообразное логирование ошибок."""
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


def first_match(pattern: re.Pattern, text: str | None) -> str | None:
    if not text:
        return None
    m = pattern.search(text)
    return m.group(0) if m else None
