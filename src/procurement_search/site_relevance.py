"""Слой 2 уточнения релевантности (design-обсуждение скоринга, см.
scoring.py про Слой 1).

Слой 1 (`scoring.compute_relevance`) сравнивает запрос со сниппетом из
поисковой выдачи — 1-2 предложения, которые сгенерировал сам источник
(Yandex/DDG/каталог), не текст с сайта компании. Здесь — краулинг
нескольких страниц САЙТА самого кандидата (`Candidate.website`) и то же
самое пересечение токенов, но по реальному тексту каталога/номенклатуры:
на порядок больше и точнее сигнала при той же простой математике.

Без embeddings — сознательное упрощение, не техническое ограничение (ранее
здесь было утверждение про SSL-блок сети в песочнице — оказалось, что pip
просто использовал системный сертификатный бандл Python.org, у которого
не был инициализирован cert.pem; чинится через certifi, сеть работает).
`compute_site_relevance` — честная детерминированная замена на пересечении
токенов (та же математика и та же токенизация, что и Слой 1 —
`scoring._tokenize`, включая стемминг, — просто на намного большем тексте).
Если понадобятся настоящие эмбеддинги — замените тело `compute_site_relevance`
на cosine similarity по вектору эмбеддинг-модели; сигнатура (текст -> float
в [0,1]) останется той же, остальной код не заметит подмены.

Применяется только к top-N кандидатам ПОСЛЕ грубого скоринга по Слою 1
(см. pipeline.py) — краулинг сайта каждого кандидата стоит времени и
HTTP-запросов, тратить это на все 100-200 кандидатов не оправдано.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from procurement_search.scoring import _tokenize
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

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAX_TEXT_CHARS = 20000


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

    # Сама найденная ссылка (Candidate.website — для активных источников,
    # google_cse.py/yandex_search.py, это URL результата поиска целиком, с
    # путём, часто прямая карточка конкретного товара) должна попасть в
    # обход ПЕРВОЙ, а не потеряться. Раньше путь молча отбрасывался —
    # оставался только "scheme://netloc", а дальше крауler шёл по общим
    # разделам сайта из _CANDIDATE_PATHS (каталог/о нас/контакты). Для
    # текста в целом это не критично (Слой 2 всё равно сравнивает по
    # токенам с других страниц тоже), но для цены товара (Слой 2, доп.,
    # см. pipeline._attach_site_price) это оказалось решающим: регекс/LLM
    # видели случайную страницу сайта, а не ту, где реально написана цена
    # ИМЕННО этого товара — отсюда цены "не с той карточки" на живой выдаче.
    urls_to_try: list[str] = []
    if parsed.path not in ("", "/") or parsed.query:
        original_url = base + parsed.path + (f"?{parsed.query}" if parsed.query else "")
        urls_to_try.append(original_url)
    for path in _CANDIDATE_PATHS:
        url = base + path
        if url not in urls_to_try:
            urls_to_try.append(url)

    texts: list[str] = []
    for url in urls_to_try:
        if len(texts) >= max_pages:
            break
        html = fetch_url(
            url,
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
