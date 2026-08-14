"""Тесты источника Yandex Search API — без сети: HTTP-сессия подменяется
фейком с заготовленными ответами (тот же паттерн, что в
test_dadata_enricher.py / test_google_cse_source.py).

XML-фикстура собрана по документации Yandex Cloud Search API (аналог
старого формата Yandex.XML) — реальный формат ответа стоит перепроверить
на первом живом ключе, см. предупреждение в docstring sources/yandex_search.py."""

import base64

import requests

from procurement_search.sources.yandex_search import YandexSearchSource, build_default

_RESULTS_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0">
  <response>
    <results>
      <grouping>
        <group>
          <doc>
            <url>https://galvanika.ru/</url>
            <title>ООО Гальваник — гальванические покрытия</title>
            <passages>
              <passage>Цинкование, хромирование. Тел: +7 900 111 22 33, email: sales@galvanika.ru</passage>
            </passages>
          </doc>
        </group>
        <group>
          <doc>
            <url>https://example.com/news/article</url>
            <title>Статья про гальванику</title>
            <passages>
              <passage>Обзорная статья без контактов конкретной компании.</passage>
            </passages>
          </doc>
        </group>
      </grouping>
    </results>
  </response>
</yandexsearch>
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class _FakeResponse:
    def __init__(self, json_data: dict, status_code: int = 200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json_data


class _FakeSession:
    """post() -> ответ с operation id; get() -> последовательность ответов
    операции (для проверки поллинга) или один ответ на все вызовы."""

    def __init__(self, post_response: _FakeResponse, get_responses: list[_FakeResponse] | _FakeResponse):
        self.post_response = post_response
        self.get_responses = get_responses if isinstance(get_responses, list) else [get_responses]
        self.post_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.post_response

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        idx = min(len(self.get_calls) - 1, len(self.get_responses) - 1)
        return self.get_responses[idx]


class _RaisingPostSession:
    def post(self, *args, **kwargs):
        raise requests.ConnectionError("Yandex Search API недоступен")


def _source(session, **kwargs) -> YandexSearchSource:
    defaults = dict(
        api_key="k",
        folder_id="f",
        session=session,
        request_delay_seconds=0.0,
        poll_interval_seconds=0.0,
        poll_timeout_seconds=2.0,
    )
    defaults.update(kwargs)
    return YandexSearchSource(**defaults)


def test_requires_api_key_and_folder_id():
    import pytest

    with pytest.raises(ValueError):
        YandexSearchSource(api_key="", folder_id="f")
    with pytest.raises(ValueError):
        YandexSearchSource(api_key="k", folder_id="")


def test_parses_results_after_operation_completes():
    session = _FakeSession(
        post_response=_FakeResponse({"id": "op123"}),
        get_responses=_FakeResponse({"done": True, "response": {"rawData": _b64(_RESULTS_XML)}}),
    )
    source = _source(session)

    candidates = source.search("гальванические покрытия")

    assert len(candidates) == 2
    first = candidates[0]
    assert first.name_raw == "ООО Гальваник — гальванические покрытия"
    assert first.website == "https://galvanika.ru/"
    assert first.phone_raw == "+7 900 111 22 33"
    assert first.email_raw == "sales@galvanika.ru"
    assert first.source == "yandex_search"

    second = candidates[1]
    assert second.phone_raw is None
    assert second.email_raw is None


def test_sends_api_key_header_and_folder_id():
    session = _FakeSession(
        post_response=_FakeResponse({"id": "op123"}),
        get_responses=_FakeResponse({"done": True, "response": {"rawData": _b64(_RESULTS_XML)}}),
    )
    source = _source(session, api_key="my-key", folder_id="my-folder")

    source.search("test")

    assert session.post_calls[0]["headers"]["Authorization"] == "Api-Key my-key"
    assert session.post_calls[0]["json"]["folderId"] == "my-folder"
    assert session.get_calls[0]["headers"]["Authorization"] == "Api-Key my-key"


def test_enriches_query_with_supplier_terms_by_default():
    session = _FakeSession(
        post_response=_FakeResponse({"id": "op123"}),
        get_responses=_FakeResponse({"done": True, "response": {"rawData": _b64(_RESULTS_XML)}}),
    )
    source = _source(session)

    source.search("генератор бензиновый 5 квт")

    sent_query = session.post_calls[0]["json"]["query"]["queryText"]
    assert sent_query == "генератор бензиновый 5 квт (поставщик | производитель | оптом)"


def test_enrich_query_can_be_disabled():
    session = _FakeSession(
        post_response=_FakeResponse({"id": "op123"}),
        get_responses=_FakeResponse({"done": True, "response": {"rawData": _b64(_RESULTS_XML)}}),
    )
    source = _source(session, enrich_query=False)

    source.search("генератор бензиновый 5 квт")

    sent_query = session.post_calls[0]["json"]["query"]["queryText"]
    assert sent_query == "генератор бензиновый 5 квт"


def test_polls_until_operation_done():
    session = _FakeSession(
        post_response=_FakeResponse({"id": "op123"}),
        get_responses=[
            _FakeResponse({"done": False}),
            _FakeResponse({"done": False}),
            _FakeResponse({"done": True, "response": {"rawData": _b64(_RESULTS_XML)}}),
        ],
    )
    source = _source(session)

    candidates = source.search("test")

    assert len(session.get_calls) == 3
    assert len(candidates) == 2


def test_operation_error_returns_empty_list():
    session = _FakeSession(
        post_response=_FakeResponse({"id": "op123"}),
        get_responses=_FakeResponse({"done": True, "error": {"code": 3, "message": "bad request"}}),
    )
    source = _source(session)

    assert source.search("test") == []


def test_missing_operation_id_returns_empty_list():
    session = _FakeSession(post_response=_FakeResponse({}), get_responses=_FakeResponse({}))
    source = _source(session)

    assert source.search("test") == []


def test_survives_network_error_without_crashing():
    source = _source(_RaisingPostSession())

    assert source.search("test") == []


def test_build_default_returns_none_without_env(monkeypatch):
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)

    assert build_default({}) is None


def test_build_default_builds_source_from_env(monkeypatch):
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "env-key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "env-folder")

    source = build_default({"yandex_search": {"max_results_per_query": 5}})

    assert source is not None
    assert source.api_key == "env-key"
    assert source.folder_id == "env-folder"
    assert source.max_results_per_query == 5
