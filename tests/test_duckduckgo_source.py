"""Тесты источника DuckDuckGo на синтетическом HTML (без сети) —
имитация типовой разметки html.duckduckgo.com/html/, включая редиректор
ссылок (uddg-параметр), который надо декодировать."""

from procurement_search.sources.base import SourceConfig
from procurement_search.sources.duckduckgo import DuckDuckGoSource, _extract_real_url

RESULTS_HTML = """
<html><body>
<div class="results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgalvanika.ru%2F&amp;rut=abc">
          ООО Гальваник — гальванические покрытия
        </a>
      </h2>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgalvanika.ru%2F">
        Цинкование, хромирование металла. Тел: +7 900 111 22 33, email: sales@galvanika.ru
      </a>
    </div>
  </div>
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews%2Farticle">
          Статья про гальванику на новостном портале
        </a>
      </h2>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews%2Farticle">
        Обзорная статья без контактов конкретной компании.
      </a>
    </div>
  </div>
</div>
</body></html>
"""


def _make_source(max_results: int = 15) -> DuckDuckGoSource:
    config = SourceConfig(
        name="duckduckgo",
        base_url="https://duckduckgo.com",
        search_url_template="https://html.duckduckgo.com/html/?q={query}",
        request_delay_seconds=0.0,
        timeout_seconds=5.0,
        user_agent="test-agent",
        respect_robots_txt=False,
        max_results_per_query=max_results,
        selectors={},
    )
    return DuckDuckGoSource(config)


def test_extract_real_url_decodes_uddg_redirect():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fgalvanika.ru%2F&rut=abc"
    assert _extract_real_url(href) == "https://galvanika.ru/"


def test_extract_real_url_passes_through_direct_link():
    assert _extract_real_url("https://galvanika.ru/") == "https://galvanika.ru/"


def test_extract_real_url_returns_none_for_empty():
    assert _extract_real_url(None) is None
    assert _extract_real_url("") is None


def test_parses_results_and_decodes_links():
    source = _make_source()
    candidates = source.parse_search_html(RESULTS_HTML)

    assert len(candidates) == 2

    galvanika = candidates[0]
    assert "Гальваник" in galvanika.name_raw
    assert galvanika.website == "https://galvanika.ru/"
    assert galvanika.source_url == "https://galvanika.ru/"
    assert galvanika.phone_raw == "+7 900 111 22 33"
    assert galvanika.email_raw == "sales@galvanika.ru"

    article = candidates[1]
    # у статьи в сниппете нет ни телефона, ни email — оба поля честно None,
    # а не выдуманы
    assert article.phone_raw is None
    assert article.email_raw is None


def test_respects_max_results_per_query():
    source = _make_source(max_results=1)
    candidates = source.parse_search_html(RESULTS_HTML)
    assert len(candidates) == 1