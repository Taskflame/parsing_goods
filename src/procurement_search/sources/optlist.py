"""Источник optlist.ru (обязателен по ТЗ, design_doc §7).

ВНИМАНИЕ: селекторы в config/sources.yaml не откалиброваны на живых
страницах (нет сетевого доступа из среды разработки). Перед боевым
использованием — см. README.md, "Калибровка селекторов".
"""

from __future__ import annotations

from urllib.parse import quote

from procurement_search.sources.base import CatalogSource, SourceConfig


class OptlistSource(CatalogSource):
    def build_search_url(self, query: str) -> str:
        return self.config.search_url_template.format(query=quote(query))


def build_default(config_dict: dict) -> OptlistSource:
    return OptlistSource(SourceConfig.from_dict("optlist", config_dict["optlist"]))
