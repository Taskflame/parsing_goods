"""Тесты источника Google CSE — без сети: HTTP-сессия подменяется фейком
с заранее заготовленным ответом (тот же паттерн, что в test_dadata_enricher.py)."""

import requests

from procurement_search.sources.google_cse import GoogleCseSource, build_default


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
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.response


class _RaisingSession:
    def get(self, *args, **kwargs):
        raise requests.ConnectionError("Google CSE недоступен")


_ITEMS_RESPONSE = {
    "items": [
        {
            "title": "ООО Гальваник — гальванические покрытия",
            "link": "https://galvanika.ru/",
            "snippet": "Цинкование, хромирование. Тел: +7 900 111 22 33, email: sales@galvanika.ru",
        },
        {
            "title": "Alibaba supplier: Electroplating Co Ltd",
            "link": "https://alibaba.com/product/electroplating-co-ltd",
            "snippet": "Zinc and chrome plating services, contact us for a quote.",
        },
    ]
}


def test_requires_api_key_and_cx():
    import pytest

    with pytest.raises(ValueError):
        GoogleCseSource(api_key="", cx="cx")
    with pytest.raises(ValueError):
        GoogleCseSource(api_key="key", cx="")


def test_parses_items_into_candidates():
    session = _FakeSession(_FakeResponse(_ITEMS_RESPONSE))
    source = GoogleCseSource(api_key="k", cx="c", session=session, request_delay_seconds=0.0)

    candidates = source.search("гальванические покрытия")

    assert len(candidates) == 2
    first = candidates[0]
    assert first.name_raw == "ООО Гальваник — гальванические покрытия"
    assert first.website == "https://galvanika.ru/"
    assert first.phone_raw == "+7 900 111 22 33"
    assert first.email_raw == "sales@galvanika.ru"
    assert first.source == "google_cse"

    second = candidates[1]
    assert "Alibaba" in second.name_raw
    assert second.website == "https://alibaba.com/product/electroplating-co-ltd"
    # у второй карточки нет телефона/email в сниппете — оба честно None
    assert second.phone_raw is None
    assert second.email_raw is None


def test_sends_key_cx_and_query():
    session = _FakeSession(_FakeResponse({"items": []}))
    source = GoogleCseSource(api_key="my-key", cx="my-cx", session=session, request_delay_seconds=0.0)

    source.search("test query")

    params = session.calls[0]["params"]
    assert params["key"] == "my-key"
    assert params["cx"] == "my-cx"
    assert params["q"] == "test query"


def test_survives_network_error_without_crashing():
    source = GoogleCseSource(api_key="k", cx="c", session=_RaisingSession(), request_delay_seconds=0.0)

    candidates = source.search("test")

    assert candidates == []


def test_missing_items_key_returns_empty_list():
    session = _FakeSession(_FakeResponse({}))
    source = GoogleCseSource(api_key="k", cx="c", session=session, request_delay_seconds=0.0)

    assert source.search("test") == []


def test_build_default_returns_none_without_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_CSE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CSE_CX", raising=False)

    assert build_default({}) is None


def test_build_default_builds_source_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CSE_API_KEY", "env-key")
    monkeypatch.setenv("GOOGLE_CSE_CX", "env-cx")

    source = build_default({"google_cse": {"max_results_per_query": 5}})

    assert source is not None
    assert source.api_key == "env-key"
    assert source.cx == "env-cx"
    assert source.max_results_per_query == 5
