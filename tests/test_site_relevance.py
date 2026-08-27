"""Тесты Слоя 2 (site_relevance.py) — без сети: HTTP-сессия подменяется
фейком, отдающим заготовленный HTML только на определённые пути (имитация
реального сайта, где не все пути из _CANDIDATE_PATHS существуют)."""

import requests

from procurement_search.scoring import _tokenize as _stemmed_tokenize
from procurement_search.site_relevance import compute_site_relevance, crawl_site_text

CATALOG_HTML = """
<html><body>
<nav>Меню</nav>
<h1>Каталог</h1>
<div class="products">Гальванические покрытия, цинкование, хромирование металла</div>
<script>console.log('игнорировать')</script>
<style>.products { color: red; }</style>
</body></html>
"""

HOME_HTML = "<html><body><h1>ООО Гальваник</h1><p>Производим покрытия с 2005 года</p></body></html>"


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    """Отдаёт HTML только на пути из `pages` (ключи — точные суффиксы после
    базового URL, например "/catalog"), для остальных — 404. Матчинг по
    точному суффиксу, а не endswith — иначе пустая строка "" (путь до
    главной страницы) матчила бы вообще любой URL."""

    def __init__(self, base: str, pages: dict[str, str]):
        self.base = base
        self.pages = pages
        self.requested_urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.requested_urls.append(url)
        path = url[len(self.base):] if url.startswith(self.base) else None
        if path is not None and path in self.pages:
            return _FakeResponse(self.pages[path])
        return _FakeResponse("", status_code=404)


def test_crawl_site_text_strips_scripts_and_styles():
    session = _FakeSession("https://galvanika.ru", {"": HOME_HTML, "/catalog": CATALOG_HTML})
    text = crawl_site_text("https://galvanika.ru", delay=0.0, respect_robots=False, session=session)

    assert text is not None
    assert "игнорировать" not in text
    assert "color: red" not in text
    assert "цинкование" in text
    assert "ООО Гальваник" in text


def test_crawl_site_text_returns_none_when_nothing_fetched():
    session = _FakeSession("https://dead-site.example", {})  # всё 404
    text = crawl_site_text("https://dead-site.example", delay=0.0, respect_robots=False, session=session)
    assert text is None


def test_crawl_site_text_respects_max_pages(monkeypatch):
    session = _FakeSession(
        "https://big-site.example",
        {p: f"<html><body>page {p}</body></html>" for p in [
            "", "/catalog", "/catalog/", "/produkciya", "/products", "/uslugi", "/services",
        ]},
    )
    text = crawl_site_text(
        "https://big-site.example", max_pages=2, delay=0.0, respect_robots=False, session=session
    )

    assert text is not None
    # не больше 2 успешных запросов должно было пойти по сети
    assert len(session.requested_urls) <= 2


def test_crawl_site_text_fetches_original_url_path_first():
    """Реальный баг с живой выдачи: Candidate.website у активных источников
    (google_cse.py/yandex_search.py) — это URL результата поиска целиком,
    часто прямая карточка конкретного товара, а не голый домен. Раньше
    crawl_site_text брал только "scheme://netloc" и путь терялся молча —
    крауler шёл по общим разделам сайта (_CANDIDATE_PATHS), карточка
    товара со специфичной информацией (например, ценой) не скачивалась
    вообще. Путь исходной ссылки должен быть запрошен, и первым."""
    product_html = "<html><body><h1>Генератор Huter DY8000L</h1><p>Цена: 48 219 ₽</p></body></html>"
    session = _FakeSession(
        "https://diptec.ru",
        {"/product/generator-benzinovyy-huter-dy8000l": product_html, "": HOME_HTML},
    )

    text = crawl_site_text(
        "https://diptec.ru/product/generator-benzinovyy-huter-dy8000l",
        delay=0.0,
        respect_robots=False,
        session=session,
    )

    assert text is not None
    assert "48 219" in text
    assert session.requested_urls[0] == "https://diptec.ru/product/generator-benzinovyy-huter-dy8000l"


def test_crawl_site_text_original_path_counts_toward_max_pages():
    session = _FakeSession(
        "https://big-site.example",
        {p: f"<html><body>page {p}</body></html>" for p in [
            "/product/123", "", "/catalog", "/catalog/", "/produkciya",
        ]},
    )
    text = crawl_site_text(
        "https://big-site.example/product/123",
        max_pages=1,
        delay=0.0,
        respect_robots=False,
        session=session,
    )

    assert text is not None
    assert "page /product/123" in text
    assert len(session.requested_urls) == 1


def test_crawl_site_text_rejects_malformed_url():
    assert crawl_site_text("не url вообще") is None


def test_compute_site_relevance_counts_token_overlap():
    # tokens — уже "как из query_tokens", т.е. стеммированные (compute_site_relevance
    # сам стеммирует только site_text, вызывающий код отвечает за то, чтобы
    # tokens были в том же формате — см. pipeline.py: tokens=query_tokens(...)).
    tokens = _stemmed_tokenize("гальванические покрытия цинкование")
    assert compute_site_relevance(tokens, "мы делаем цинкование и гальванические покрытия") == 1.0
    assert compute_site_relevance(tokens, "продажа тортов и пирожных") == 0.0


def test_compute_site_relevance_handles_empty_input():
    assert compute_site_relevance(set(), "любой текст") == 0.0
    assert compute_site_relevance({"токен"}, "") == 0.0
