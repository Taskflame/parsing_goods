from datetime import date

from procurement_search.models import Candidate, Company, FieldValue, VerificationFlag
from procurement_search.query_normalizer import NormalizedQuery
from procurement_search.scoring import (
    combine_score,
    compute_confidence,
    compute_relevance,
    compute_score,
    compute_trust,
)

WEIGHTS = {
    "default": {
        "relevance_exponent": 0.6,
        "trust_exponent": 0.4,
        "confidence_base": 0.5,
        "trust_prior": 0.5,
        "trust_shrinkage": 0.3,
        "trust_signal_weights": {
            "status": 0.35,
            "website_alive": 0.15,
            "inn_resolved": 0.15,
            "years_in_business": 0.15,
            "employees": 0.10,
            "revenue": 0.10,
        },
    },
}


def _company(
    name: str,
    description: str | None = None,
    status: str = "неизвестно",
    inn: str | None = None,
) -> Company:
    candidate = Candidate(
        source="test",
        source_url="https://example.com",
        name_raw=name,
        description_raw=description,
        scraped_at=date(2026, 8, 6),
    )
    return Company(
        inn=inn,
        ogrn=None,
        name=FieldValue(name, "test", date(2026, 8, 6), VerificationFlag.UNVERIFIED),
        status=status,
        raw_candidates=[candidate],
    )


def test_relevance_is_higher_for_matching_terms():
    query = NormalizedQuery(
        raw_query="гальванические покрытия",
        category="Гальванические_покрытия",
        search_terms=["гальванические покрытия", "цинкование", "анодирование"],
    )
    relevant = _company("Завод гальванических покрытий", "цинкование, хромирование металла")
    irrelevant = _company("Кондитерская фабрика", "производство тортов")

    assert compute_relevance(relevant, query) > compute_relevance(irrelevant, query)


def test_compute_score_uses_category_weights_and_stays_in_unit_range():
    query = NormalizedQuery(
        raw_query="гальванические покрытия",
        category="Гальванические_покрытия",
        search_terms=["гальванические покрытия"],
    )
    # прототип матчит по точным токенам, без морфологии/стемминга (design_doc
    # §11) — поэтому здесь словоформы совпадают с search_terms дословно
    company = _company("ООО Гальванические покрытия", status="действующая", inn="7700000000")
    score = compute_score(company, query, weights=WEIGHTS)

    assert 0.0 <= score.total <= 1.0
    assert score.relevance > 0.0


def test_active_company_has_higher_trust_than_liquidated():
    active = _company("Активная компания", status="действующая", inn="1111111111")
    liquidated = _company("Ликвидированная компания", status="ликвидирована")

    assert compute_trust(active, WEIGHTS["default"]) > compute_trust(liquidated, WEIGHTS["default"])


def test_trust_never_hits_zero_from_pure_absence_of_data():
    # Без DADATA_API_KEY (NullEnricher) status всегда "неизвестно", inn
    # всегда None — trust не должен схлопываться в 0.0 из-за этого,
    # иначе (relevance^a) × trust^b обнулял бы КАЖДЫЙ score, пока не
    # подключён реальный резолвинг в ЕГРЮЛ. Это и есть усадка к
    # trust_prior (см. scoring._weighted_average).
    company = _company("Компания без данных", status="неизвестно")
    trust = compute_trust(company, WEIGHTS["default"])
    assert trust > 0.0


def test_liquidated_company_gets_low_trust():
    # Основная гарантия "ликвидированные не в топе" — knockout в
    # pipeline._is_knockout (тестируется отдельно в test_pipeline.py),
    # который убирает такие компании ДО скоринга. compute_trust здесь
    # проверяется в изоляции (в обход пайплайна) — статус "ликвидирована"
    # должен тянуть trust заметно ниже нейтрального prior (0.5), но не
    # обязан быть ровно нулём из-за усадки к среднему.
    query = NormalizedQuery(raw_query="x", category=None, search_terms=["x"])
    company = _company("Любая компания", status="ликвидирована")
    score = compute_score(company, query, weights=WEIGHTS)
    assert score.trust < 0.3


def test_confidence_reflects_filled_fields():
    empty = _company("Пустая карточка")
    full = _company("Полная карточка", inn="1111111111", status="действующая")
    full.contacts = {
        "phone": [FieldValue("+7 900 000 00 00", "test", date(2026, 8, 6), VerificationFlag.UNVERIFIED)],
        "email": [FieldValue("info@test.ru", "test", date(2026, 8, 6), VerificationFlag.UNVERIFIED)],
        "address": [FieldValue("Москва", "test", date(2026, 8, 6), VerificationFlag.UNVERIFIED)],
        "contact_person": [FieldValue("Иванов", "test", date(2026, 8, 6), VerificationFlag.UNVERIFIED)],
        "website": [FieldValue("https://test.ru", "test", date(2026, 8, 6), VerificationFlag.CONFIRMED)],
    }

    assert compute_confidence(full) > compute_confidence(empty)
    assert compute_confidence(empty) == 0.0


def test_combine_score_zero_relevance_dominates_high_trust():
    # Мультипликативно: нулевая релевантность должна топить итог,
    # независимо от того, насколько высокий trust — это и была главная
    # претензия к старой аддитивной формуле (design-обсуждение скоринга).
    w = WEIGHTS["default"]
    zero_relevance = combine_score(relevance=0.0, trust=0.95, confidence=1.0, weights=w)
    modest_relevance = combine_score(relevance=0.3, trust=0.4, confidence=1.0, weights=w)

    assert zero_relevance == 0.0
    assert modest_relevance > zero_relevance
