"""Слой 2 уточнения релевантности (design-обсуждение скоринга, см.
scoring.py про Слой 1).

Слой 1 (`scoring.compute_relevance`) сравнивает запрос со сниппетом из
поисковой выдачи — 1-2 предложения, которые сгенерировал сам источник
(Yandex/DDG/каталог), не текст с сайта компании. Здесь — краулинг
нескольких страниц САЙТА самого кандидата (`Candidate.website`) и то же
самое пересечение токенов, но по реальному тексту каталога/номенклатуры:
на порядок больше и точнее сигнала при той же простой математике.

Без embeddings — осознанное решение по факту, а не заглушка от лени: pip
в этой среде разработки не может поставить ничего с PyPI (SSL-блок сети,
проверено), значит sentence-transformers/torch тут физически не завести и
не протестировать. `compute_site_relevance` — честная детерминированная
замена на пересечении токенов (та же математика, что и Слой 1, просто на
намного большем тексте). Если код запускается вне этой песочницы и нужны
настоящие эмбеддинги — замените тело `compute_site_relevance` на cosine
similarity по вектору эмбеддинг-модели; сигнатура (текст -> float в [0,1])
останется той же, остальной код не заметит подмены.

Применяется только к top-N кандидатам ПОСЛЕ грубого скоринга по Слою 1
(см. pipeline.py) — краулинг сайта каждого кандидата стоит времени и
HTTP-запросов, тратить это на все 100-200 кандидатов не оправдано.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from procurement_search.sources.base import fetch_url

logger = logging.getLogger(__name__)

# Типовые разделы, где у B2B-сайта живёт номенклатура — не тащим весь
# сайт (design-обсуждение прямо предупреждает: дальше начинается блог и
# новости, которые только разбавляют сигнал). "" — главная страница.
_CANDIDATE_PATHS = [
    "",
    "/catalog",
    "/catalog/",
    "/produkciya",
    "/products",
    "/uslugi",
    "/services",
    "/about",
    "/o-nas",
    "/kontakty",
    "/contacts",
]

# См. scoring.py::_TOKEN_RE — тот же порядок альтернатив и та же причина:
# дробное число раньше отдельных цифр, иначе "2,5" -> "2", "5".
_TOKEN_RE = re.compile(r"[а-яa-zё]+|\d+[.,]\d+|\d+", re.IGNORECASE)
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAX_TEXT_CHARS = 20000


def _tokenize(text: str) -> set[str]:
    return {t.lower().replace(",", ".") for t in _TOKEN_RE.findall(text)}


def _extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def crawl_site_text(
    website_url: str,
    max_pages: int = 6,
    timeout: float = 10.0,
    delay: float = 1.0,
    user_agent: str = _DEFAULT_UA,
    respect_robots: bool = True,
    session: requests.Session | None = None,
) -> str | None:
    """Скачивает до `max_pages` ключевых страниц сайта кандидата и
    возвращает объединённый видимый текст (без <script>/<style>). None,
    если ни одна страница не отдалась — не выдумываем текст, честно
    сигнализируем "нечего анализировать" вызывающему коду."""
    parsed = urlparse(website_url)
    if not parsed.scheme or not parsed.netloc:
        logger.warning("Не похоже на URL сайта компании: %r", website_url)
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"

    texts: list[str] = []
    for path in _CANDIDATE_PATHS:
        if len(texts) >= max_pages:
            break
        html = fetch_url(
            base + path,
            user_agent=user_agent,
            timeout=timeout,
            delay=delay,
            respect_robots=respect_robots,
            session=session,
        )
        if html is None:
            continue
        text = _extract_visible_text(html)
        if text:
            texts.append(text)

    if not texts:
        return None
    return " ".join(texts)[:_MAX_TEXT_CHARS]


def compute_site_relevance(tokens: set[str], site_text: str) -> float:
    """Та же математика, что scoring.compute_relevance (Слой 1) — доля
    токенов запроса, встретившихся в тексте — но на реальном тексте сайта
    вместо сниппета поисковой выдачи."""
    if not tokens or not site_text:
        return 0.0
    site_tokens = _tokenize(site_text)
    overlap = len(tokens & site_tokens)
    return min(1.0, overlap / len(tokens))
