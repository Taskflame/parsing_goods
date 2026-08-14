"""Тесты парсинга источников на синтетическом HTML (без сети).

pulscen.ru и optlist.ru недоступны из среды разработки (design_doc §11),
поэтому здесь проверяется сама логика извлечения (JSON-LD и CSS-fallback),
а не соответствие реальной вёрстке этих сайтов. Фикстуры — вручную
собранный HTML, имитирующий типовую разметку подобных каталогов.
"""

from procurement_search.sources.base import CatalogSource, SourceConfig

JSONLD_HTML = """
<html><body>
<div id="results">
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Organization",
    "name": "ООО Ромашка",
    "telephone": "+7 900 123 45 67",
    "email": "sales@romashka.ru",
    "url": "https://romashka.ru",
    "address": {"@type": "PostalAddress", "streetAddress": "г. Москва, ул. Ленина, 1"},
    "description": "Гальванические покрытия, цинкование, хромирование"
  }
  </script>
</div>
</body></html>
"""

CSS_FALLBACK_HTML = """
<html><body>
<div class="company-item">
  <a class="company-item__name" href="/company/vasilek">ООО Василёк</a>
  <div class="company-item__phone">Тел: 8 (495) 123-45-67</div>
  <div class="company-item__address">г. Санкт-Петербург, Невский пр., 10</div>
  <div class="company-item__description">Поставки химикатов для гальваники</div>
</div>
</body></html>
"""


def _make_source() -> CatalogSource:
    config = SourceConfig(
        name="pulscen",
        base_url="https://www.pulscen.ru",
        search_url_template="https://www.pulscen.ru/search?query={query}",
        request_delay_seconds=0.0,
        timeout_seconds=5.0,
        user_agent="test-agent",
        respect_robots_txt=False,
        max_results_per_query=30,
        selectors={
            "item_selector": "div.company-item",
            "name_selector": "a.company-item__name",
            "link_selector": "a.company-item__name",
            "phone_selector": ".company-item__phone",
            "address_selector": ".company-item__address",
            "description_selector": ".company-item__description",
        },
    )

    class _TestSource(CatalogSource):
        def build_search_url(self, query: str) -> str:
            return self.config.search_url_template.format(query=query)

    return _TestSource(config)


def test_parses_jsonld_organization():
    source = _make_source()
    candidates = source.parse_search_html(JSONLD_HTML, source_url="https://www.pulscen.ru/search?query=x")

    assert len(candidates) == 1
    c = candidates[0]
    assert c.name_raw == "ООО Ромашка"
    assert c.phone_raw == "+7 900 123 45 67"
    assert c.email_raw == "sales@romashka.ru"
    assert "Москва" in c.address_raw


def test_falls_back_to_css_selectors_when_no_jsonld():
    source = _make_source()
    candidates = source.parse_search_html(
        CSS_FALLBACK_HTML, source_url="https://www.pulscen.ru/search?query=x"
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.name_raw == "ООО Василёк"
    assert "8 (495) 123-45-67" in c.phone_raw
    assert c.source_url == "https://www.pulscen.ru/company/vasilek"


def test_css_fallback_extracts_phone_by_regex_if_no_dedicated_element():
    config_html = """
    <div class="company-item">
      <a class="company-item__name" href="/c/1">Компания без явного телефона</a>
      <div class="company-item__description">Звоните: +7 981 555 66 77, пишите на info@example.com</div>
    </div>
    """
    source = _make_source()
    candidates = source.parse_search_html(config_html, source_url="https://www.pulscen.ru/search?query=x")

    assert len(candidates) == 1
    c = candidates[0]
    assert c.phone_raw == "+7 981 555 66 77"
    assert c.email_raw == "info@example.com"
