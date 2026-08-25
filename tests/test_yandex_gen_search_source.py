"""Тесты источника Yandex gen-search — без сети: HTTP-сессия подменяется
фейком (тот же паттерн, что в test_yandex_search_source.py).

Фикстура ответа собрана по РЕАЛЬНОМУ живому ответу API (проверено вживую
на реальном ключе в design-обсуждении, не только по документации/proto,
в отличие от классического yandex_search.py)."""

import pytest
import requests

from procurement_search.sources.yandex_gen_search import YandexGenSearchSource, build_default

_LIVE_RESPONSE = [
    {
        "message": {
            "content": "Мотоцикл IRBIS KTR 250 можно найти на нескольких сайтах...",
            "role": "ROLE_ASSISTANT",
        },
        "sources": [
            {
                "used": True,
                "url": "https://irbismotors.ru/catalog/mototsikly/2368/",
                "title": "Купить мотоцикл эндуро KTR250 | IRBIS MOTORS",
            },
            {
                "used": True,
                "url": "https://darexmoto.ru/catalog/enduro-irbis-ktr-250/",
                "title": "Мотоцикл эндуро IRBIS KTR 250 купить в DarexMoto г. Москва",
            },
            {
                "used": False,
                "url": "https://www.avito.ru/all/mototsikly_i_mototehnika/mototsikly/kross_i_enduro",
                "title": "оптом -",
            },
            {
                "used": False,
                "url": "https://www.pulscen.ru/price/280501-motocikly/f:30324_irbis&31348_optom",
                "title": "Мотоциклы Irbis оптом в РОССИИ по выгодной цене - купить на Пульсе цен",
            },
        ],
        "isAnswerRejected": False,
        "isBulletAnswer": False,
        "problematicAnswer": False,
    }
]


class _FakeResponse:
    def __init__(self, json_data, status_code: int = 200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.post_calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.response


class _RaisingSession:
    def post(self, *args, **kwargs):
        raise requests.ConnectionError("Yandex gen-search недоступен")


def _source(session, **kwargs) -> YandexGenSearchSource:
    defaults = dict(api_key="k", folder_id="f", session=session, request_delay_seconds=0.0)
    defaults.update(kwargs)
    return YandexGenSearchSource(**defaults)


def test_requires_api_key_and_folder_id():
    with pytest.raises(ValueError):
        YandexGenSearchSource(api_key="", folder_id="f")
    with pytest.raises(ValueError):
        YandexGenSearchSource(api_key="k", folder_id="")


def test_only_returns_sources_the_llm_actually_cited_by_default():
    session = _FakeSession(_FakeResponse(_LIVE_RESPONSE))
    source = _source(session)

    candidates = source.search("мотоцикл кроссовый IRBIS 250 кубов")

    # avito.ru/pulscen.ru были в sources[], но used=False -> отфильтрованы
    urls = [c.source_url for c in candidates]
    assert urls == [
        "https://irbismotors.ru/catalog/mototsikly/2368/",
        "https://darexmoto.ru/catalog/enduro-irbis-ktr-250/",
    ]
    assert all(c.website == c.source_url for c in candidates)
    assert all(c.source == "yandex_gen_search" for c in candidates)


def test_can_include_unused_sources_when_disabled():
    session = _FakeSession(_FakeResponse(_LIVE_RESPONSE))
    source = _source(session, only_used_sources=False)

    candidates = source.search("мотоцикл кроссовый IRBIS 250 кубов")

    assert len(candidates) == 4


def test_sends_api_key_header_and_folder_id_and_query():
    session = _FakeSession(_FakeResponse(_LIVE_RESPONSE))
    source = _source(session, api_key="my-key", folder_id="my-folder")

    source.search("test query")

    call = session.post_calls[0]
    assert call["headers"]["Authorization"] == "Api-Key my-key"
    assert call["json"]["folderId"] == "my-folder"
    assert call["json"]["messages"] == [{"role": "ROLE_USER", "content": "test query"}]


def test_respects_max_results_per_query():
    session = _FakeSession(_FakeResponse(_LIVE_RESPONSE))
    source = _source(session, max_results_per_query=1)

    candidates = source.search("test")

    assert len(candidates) == 1


def test_survives_network_error_without_crashing():
    source = _source(_RaisingSession())

    candidates = source.search("test")

    assert candidates == []


def test_survives_empty_response_array():
    session = _FakeSession(_FakeResponse([]))
    source = _source(session)

    assert source.search("test") == []


def test_build_default_none_without_enabled_flag(monkeypatch):
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "k")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "f")
    monkeypatch.delenv("YANDEX_GEN_SEARCH_ENABLED", raising=False)

    assert build_default({}) is None


def test_build_default_none_without_keys_even_if_enabled(monkeypatch):
    monkeypatch.setenv("YANDEX_GEN_SEARCH_ENABLED", "true")
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)

    assert build_default({}) is None


def test_build_default_creates_source_when_enabled_and_keyed(monkeypatch):
    monkeypatch.setenv("YANDEX_GEN_SEARCH_ENABLED", "true")
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "k")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "f")

    source = build_default({})

    assert source is not None
    assert source.name == "yandex_gen_search"
