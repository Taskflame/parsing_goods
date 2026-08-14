from datetime import date

from procurement_search.dedup import dedup_candidates, extract_domain, normalize_name, normalize_phone
from procurement_search.models import Candidate


def _cand(**kwargs) -> Candidate:
    defaults = dict(
        source="test",
        source_url="https://example.com",
        name_raw="ООО Ромашка",
        scraped_at=date(2026, 8, 6),
    )
    defaults.update(kwargs)
    return Candidate(**defaults)


def test_normalize_name_strips_legal_form_and_punctuation():
    assert normalize_name('ООО "Ромашка"') == "ромашка"
    assert normalize_name("Ромашка ООО") == "ромашка"


def test_normalize_phone_takes_last_ten_digits():
    assert normalize_phone("+7 (900) 123-45-67") == "9001234567"
    assert normalize_phone("8 900 123 45 67") == "9001234567"
    assert normalize_phone(None) is None


def test_extract_domain_from_email_and_website():
    assert extract_domain("sales@romashka.ru", None) == "romashka.ru"
    assert extract_domain(None, "https://www.romashka.ru/about") == "romashka.ru"
    assert extract_domain(None, None) is None


def test_dedup_merges_by_matching_phone_despite_different_names():
    a = _cand(source="pulscen", name_raw='ООО "Ромашка"', phone_raw="+7 900 123 45 67")
    b = _cand(source="optlist", name_raw="Ромашка", phone_raw="8 (900) 123-45-67")
    groups = dedup_candidates([a, b])
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_dedup_keeps_distinct_companies_separate():
    a = _cand(name_raw="Ромашка", phone_raw="+7 900 111 11 11")
    b = _cand(name_raw="Василёк", phone_raw="+7 900 222 22 22")
    groups = dedup_candidates([a, b])
    assert len(groups) == 2


def test_dedup_does_not_merge_by_shared_catalog_listing_domain():
    # Регрессия: source_url у обоих кандидатов указывает на один и тот же
    # каталог (pulscen.ru), но это разные компании с разными карточками —
    # раньше домен ошибочно брался из source_url и все компании с одного
    # источника схлопывались в одну (см. models.Candidate.website).
    a = _cand(
        name_raw="Ромашка",
        source_url="https://www.pulscen.ru/company/romashka",
        phone_raw="+7 900 111 11 11",
    )
    b = _cand(
        name_raw="Василёк",
        source_url="https://www.pulscen.ru/company/vasilek",
        phone_raw="+7 900 222 22 22",
    )
    groups = dedup_candidates([a, b])
    assert len(groups) == 2


def test_dedup_merges_by_matching_website_domain():
    a = _cand(source="pulscen", name_raw="Ромашка Трейд", website="https://romashka.ru")
    b = _cand(source="optlist", name_raw="Romashka Group", website="https://www.romashka.ru/opt")
    groups = dedup_candidates([a, b])
    assert len(groups) == 1
