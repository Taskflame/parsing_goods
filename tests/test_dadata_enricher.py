"""Тесты DadataEnricher — без сети: HTTP-сессия подменяется фейком,
который возвращает заранее заготовленные ответы Dadata suggest API."""

from datetime import date

import pytest
import requests

from procurement_search.enrichment import DadataEnricher
from procurement_search.models import Candidate, VerificationFlag


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
    """Возвращает по одному заготовленному ответу на каждый .post(); если
    задан только один — отдаёт его на все вызовы."""

    def __init__(self, responses: list[_FakeResponse] | _FakeResponse):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


class _RaisingSession:
    def post(self, *args, **kwargs):
        raise requests.ConnectionError("Dadata недоступна")


def _cand(**kwargs) -> Candidate:
    defaults = dict(
        source="pulscen",
        source_url="https://www.pulscen.ru/company/1",
        name_raw="ООО Ромашка",
        scraped_at=date(2026, 8, 7),
    )
    defaults.update(kwargs)
    return Candidate(**defaults)


def _suggestion(inn="7700000000", ogrn="1027700000000", status="ACTIVE", full_name='ООО "РОМАШКА"', address=None):
    return {
        "value": full_name,
        "data": {
            "inn": inn,
            "ogrn": ogrn,
            "name": {"full_with_opf": full_name, "short_with_opf": full_name},
            "state": {"status": status},
            "address": {"value": address} if address else None,
        },
    }


def test_requires_api_key():
    with pytest.raises(ValueError):
        DadataEnricher(api_key="")


def test_resolves_active_company():
    session = _FakeSession(_FakeResponse({"suggestions": [_suggestion(status="ACTIVE")]}))
    enricher = DadataEnricher(api_key="test-key", session=session)

    company = enricher.build_company([_cand()])

    assert company.inn == "7700000000"
    assert company.ogrn == "1027700000000"
    assert company.status == "действующая"
    assert company.name.value == 'ООО "РОМАШКА"'
    assert company.name.source == "ЕГРЮЛ (Dadata)"
    assert company.name.confidence == VerificationFlag.CONFIRMED

    # запрос ушёл с правильным токеном
    assert session.calls[0]["headers"]["Authorization"] == "Token test-key"


def test_liquidated_company_gets_correct_status():
    session = _FakeSession(_FakeResponse({"suggestions": [_suggestion(status="LIQUIDATED")]}))
    enricher = DadataEnricher(api_key="test-key", session=session)

    company = enricher.build_company([_cand()])

    assert company.status == "ликвидирована"


def test_unmatched_company_stays_unconfirmed_lead():
    session = _FakeSession(_FakeResponse({"suggestions": []}))
    enricher = DadataEnricher(api_key="test-key", session=session)

    company = enricher.build_company([_cand(name_raw="Совершенно неизвестная контора")])

    assert company.inn is None
    assert company.status == "неизвестно"
    assert company.name.confidence == VerificationFlag.UNVERIFIED


def test_survives_network_error_without_crashing():
    enricher = DadataEnricher(api_key="test-key", session=_RaisingSession())

    company = enricher.build_company([_cand()])

    assert company.inn is None
    assert company.status == "неизвестно"


def test_egrul_address_added_with_confirmed_flag_at_front():
    session = _FakeSession(
        _FakeResponse(
            {"suggestions": [_suggestion(status="ACTIVE", address="г. Москва, ул. Ленина, 1")]}
        )
    )
    enricher = DadataEnricher(api_key="test-key", session=session)

    company = enricher.build_company(
        [_cand(address_raw="Москва, Ленина 1 (со слов сайта)")]
    )

    addresses = company.contacts["address"]
    assert len(addresses) == 2
    assert addresses[0].value == "г. Москва, ул. Ленина, 1"
    assert addresses[0].confidence == VerificationFlag.CONFIRMED
    assert addresses[1].value == "Москва, Ленина 1 (со слов сайта)"
    assert addresses[1].confidence == VerificationFlag.UNVERIFIED


def test_picks_suggestion_whose_address_matches_candidate():
    # Два тёзки с одинаковым названием, но в разных городах — должны
    # выбрать того, чей адрес пересекается с адресом кандидата.
    session = _FakeSession(
        _FakeResponse(
            {
                "suggestions": [
                    _suggestion(inn="1111111111", address="г. Новосибирск, ул. Ленина, 1"),
                    _suggestion(inn="2222222222", address="г. Москва, ул. Ленина, 1"),
                ]
            }
        )
    )
    enricher = DadataEnricher(api_key="test-key", session=session)

    company = enricher.build_company(
        [_cand(address_raw="г. Москва, ул. Ленина, д.1")]
    )

    assert company.inn == "2222222222"


def test_contacts_from_scraping_stay_unverified():
    session = _FakeSession(_FakeResponse({"suggestions": [_suggestion(status="ACTIVE")]}))
    enricher = DadataEnricher(api_key="test-key", session=session)

    company = enricher.build_company(
        [_cand(phone_raw="+7 900 111 11 11", email_raw="info@romashka.ru")]
    )

    assert company.contacts["phone"][0].confidence == VerificationFlag.UNVERIFIED
    assert company.contacts["email"][0].confidence == VerificationFlag.UNVERIFIED