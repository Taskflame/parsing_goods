"""End-to-end прогон пайплайна без сети: сетевые вызовы источников
подменяются, чтобы протестировать normalize -> dedup -> score -> export
как единое целое (design_doc §3)."""

from datetime import date

from procurement_search.enrichment import DadataEnricher, Enricher, NullEnricher
from procurement_search.models import Candidate, Company, FieldValue, VerificationFlag
from procurement_search import pipeline
from procurement_search.pipeline import run_pipeline, search_and_score
from procurement_search.sources.duckduckgo import DuckDuckGoSource
from procurement_search.sources.optlist import OptlistSource
from procurement_search.sources.pulscen import PulscenSource

import openpyxl


def _fake_candidates(source_name: str) -> list[Candidate]:
    return [
        Candidate(
            source=source_name,
            source_url=f"https://{source_name}.example/company/1",
            name_raw="ООО Гальванические покрытия",
            phone_raw="+7 900 111 11 11",
            email_raw="info@galvanika.ru",
            address_raw="г. Москва",
            description_raw="цинкование хромирование анодирование",
        ),
        Candidate(
            source=source_name,
            source_url=f"https://{source_name}.example/company/2",
            name_raw="Кондитерская фабрика Сладость",
            phone_raw="+7 900 222 22 22",
            description_raw="производство тортов и конфет",
        ),
    ]


def test_run_pipeline_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(PulscenSource, "search", lambda self, query: _fake_candidates("pulscen"))
    monkeypatch.setattr(OptlistSource, "search", lambda self, query: _fake_candidates("optlist"))
    monkeypatch.setattr(DuckDuckGoSource, "search", lambda self, query: [])

    output = tmp_path / "out.xlsx"
    result_path = run_pipeline("гальванические покрытия", output)

    assert result_path == output
    assert output.exists()

    wb = openpyxl.load_workbook(output)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))

    # дубли по телефону/названию с pulscen и optlist схлопнуты в 2 компании
    assert len(rows) == 2

    names = [r[0] for r in rows]
    assert "ООО Гальванические покрытия" in names
    assert "Кондитерская фабрика Сладость" in names

    # релевантная компания (совпадение с запросом) ранжирована выше нерелевантной
    assert rows[0][0] == "ООО Гальванические покрытия"
    galvanika_score = rows[0][-1]
    candy_score = rows[1][-1]
    assert galvanika_score > candy_score


class _StatusByNameEnricher(Enricher):
    """Тестовый Enricher: статус компании определяется по названию
    кандидата — имитирует то, что DadataEnricher делал бы по данным ЕГРЮЛ,
    без реального обращения к Dadata API."""

    def build_company(self, candidate_group):
        primary = candidate_group[0]
        status = "ликвидирована" if "Закрыто" in primary.name_raw else "действующая"
        return Company(
            inn="1234567890",
            ogrn=None,
            name=FieldValue(
                primary.name_raw, primary.source, primary.scraped_at, VerificationFlag.CONFIRMED
            ),
            status=status,
            sources=[primary.source],
            raw_candidates=candidate_group,
        )


def test_search_and_score_excludes_liquidated_companies(monkeypatch):
    def fake_candidates(source_name: str) -> list[Candidate]:
        return [
            Candidate(
                source=source_name,
                source_url="https://x.example/1",
                name_raw="ООО Живая Компания",
                description_raw="гальванические покрытия",
            ),
            Candidate(
                source=source_name,
                source_url="https://x.example/2",
                name_raw="ООО Закрыто Давно",
                description_raw="гальванические покрытия",
            ),
        ]

    monkeypatch.setattr(PulscenSource, "search", lambda self, query: fake_candidates("pulscen"))
    monkeypatch.setattr(OptlistSource, "search", lambda self, query: [])
    monkeypatch.setattr(DuckDuckGoSource, "search", lambda self, query: [])

    companies = search_and_score(
        "гальванические покрытия", enricher=_StatusByNameEnricher()
    )

    names = [c.name.value for c in companies]
    assert "ООО Живая Компания" in names
    assert "ООО Закрыто Давно" not in names


def test_website_liveness_attached_when_candidate_has_website(monkeypatch):
    def fake_candidates(source_name: str) -> list[Candidate]:
        return [
            Candidate(
                source=source_name,
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="гальванические покрытия",
                website="https://galvanika.ru",
            ),
        ]

    monkeypatch.setattr(PulscenSource, "search", lambda self, query: fake_candidates("pulscen"))
    monkeypatch.setattr(OptlistSource, "search", lambda self, query: [])
    monkeypatch.setattr(DuckDuckGoSource, "search", lambda self, query: [])
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )

    companies = search_and_score("гальванические покрытия")

    assert len(companies) == 1
    website_field = companies[0].contacts["website"][0]
    assert website_field.value == "https://galvanika.ru"
    assert website_field.confidence == VerificationFlag.CONFIRMED


def test_website_liveness_skipped_when_disabled(monkeypatch):
    def fake_candidates(source_name: str) -> list[Candidate]:
        return [
            Candidate(
                source=source_name,
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="гальванические покрытия",
                website="https://galvanika.ru",
            ),
        ]

    def fail_if_called(url, **kwargs):
        raise AssertionError("check_website_liveness не должен вызываться при verify_websites=False")

    monkeypatch.setattr(PulscenSource, "search", lambda self, query: fake_candidates("pulscen"))
    monkeypatch.setattr(OptlistSource, "search", lambda self, query: [])
    monkeypatch.setattr(DuckDuckGoSource, "search", lambda self, query: [])
    monkeypatch.setattr(pipeline, "check_website_liveness", fail_if_called)

    companies = search_and_score("гальванические покрытия", verify_websites=False)

    assert "website" not in companies[0].contacts


def test_default_enricher_is_null_without_dadata_key(monkeypatch):
    monkeypatch.delenv("DADATA_API_KEY", raising=False)
    assert isinstance(pipeline._default_enricher(), NullEnricher)


def test_default_enricher_is_dadata_when_key_present(monkeypatch):
    monkeypatch.setenv("DADATA_API_KEY", "some-key")
    assert isinstance(pipeline._default_enricher(), DadataEnricher)


def test_knockout_excludes_company_with_dead_website(monkeypatch):
    # STALE (обрыв соединения/DNS) — сильный сигнал, что сайт компании
    # не отвечает технически, а не просто заблокировал бота (см.
    # verify_contacts.py про разницу с 4xx/5xx). pipeline._is_knockout
    # должен убрать такую компанию ДО скоринга, не просто занизить score.
    def fake_candidates(source_name: str) -> list[Candidate]:
        return [
            Candidate(
                source=source_name,
                source_url="https://x.example/1",
                name_raw="ООО Протухший Сайт",
                description_raw="гальванические покрытия",
                website="https://dead.example",
            ),
        ]

    monkeypatch.setattr(PulscenSource, "search", lambda self, query: fake_candidates("pulscen"))
    monkeypatch.setattr(OptlistSource, "search", lambda self, query: [])
    monkeypatch.setattr(DuckDuckGoSource, "search", lambda self, query: [])
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.STALE
    )

    companies = search_and_score("гальванические покрытия")

    assert companies == []


def test_deep_relevance_refines_score_using_crawled_site_text(monkeypatch):
    def fake_candidates(source_name: str) -> list[Candidate]:
        return [
            Candidate(
                source=source_name,
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="",  # снипет пустой — Слой 1 не найдёт ничего
                website="https://galvanika.example",
            ),
        ]

    monkeypatch.setattr(PulscenSource, "search", lambda self, query: fake_candidates("pulscen"))
    monkeypatch.setattr(OptlistSource, "search", lambda self, query: [])
    monkeypatch.setattr(DuckDuckGoSource, "search", lambda self, query: [])
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(
        pipeline,
        "crawl_site_text",
        lambda url, **kwargs: "у нас есть цинкование и гальванические покрытия",
    )

    companies_shallow = search_and_score("гальванические покрытия", deep_relevance=False)
    companies_deep = search_and_score("гальванические покрытия", deep_relevance=True)

    assert companies_shallow[0].score.relevance == 0.0
    assert companies_deep[0].score.relevance > 0.0


def test_deep_relevance_llm_check_penalizes_negative_verdict(monkeypatch):
    def fake_candidates(source_name: str) -> list[Candidate]:
        return [
            Candidate(
                source=source_name,
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="",
                website="https://galvanika.example",
            ),
        ]

    monkeypatch.setattr(PulscenSource, "search", lambda self, query: fake_candidates("pulscen"))
    monkeypatch.setattr(OptlistSource, "search", lambda self, query: [])
    monkeypatch.setattr(DuckDuckGoSource, "search", lambda self, query: [])
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(
        pipeline,
        "crawl_site_text",
        lambda url, **kwargs: "у нас есть цинкование и гальванические покрытия",
    )
    monkeypatch.setattr(pipeline, "classify_relevance", lambda raw_query, site_text: False)

    without_llm = search_and_score("гальванические покрытия", deep_relevance=True)
    with_llm = search_and_score(
        "гальванические покрытия", deep_relevance=True, relevance_llm_check=True
    )

    assert with_llm[0].score.relevance < without_llm[0].score.relevance


def test_attach_site_contacts_extracts_phone_email_address():
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )
    site_text = (
        "Контакты: г. Москва, ул. Ленина, д. 5, оф. 10. "
        "Тел: +7 900 123-45-67, email: sales@example.ru"
    )

    pipeline._attach_site_contacts(company, site_text)

    assert company.contacts["phone"][0].value == "+7 900 123-45-67"
    assert company.contacts["email"][0].value == "sales@example.ru"
    assert company.contacts["address"][0].value == "г. Москва, ул. Ленина, д. 5, оф. 10"
    assert company.contacts["phone"][0].confidence == VerificationFlag.UNVERIFIED
    assert company.contacts["phone"][0].source == "текст сайта (Слой 2)"


def test_attach_site_contacts_prioritizes_over_existing_contact():
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
        contacts={
            "phone": [FieldValue("8 111 111-11-11", "yandex_search", date.today(), VerificationFlag.UNVERIFIED)]
        },
    )
    site_text = "Тел: +7 900 123-45-67"

    pipeline._attach_site_contacts(company, site_text)

    # insert(0, ...): контакт с реального сайта — первый (export/webapp
    # показывают только contacts[field][0]), старый снипет-контакт остался
    # вторым, не потерян.
    assert company.contacts["phone"][0].value == "+7 900 123-45-67"
    assert company.contacts["phone"][1].value == "8 111 111-11-11"


def test_attach_site_contacts_no_match_leaves_contacts_untouched():
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )

    pipeline._attach_site_contacts(company, "Продаём генераторы 5 квт оптом и в розницу")

    assert company.contacts.get("phone") is None
    assert company.contacts.get("email") is None
    assert company.contacts.get("address") is None


def test_deep_relevance_populates_contacts_from_crawled_site(monkeypatch):
    def fake_candidates(source_name: str) -> list[Candidate]:
        return [
            Candidate(
                source=source_name,
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="",
                website="https://galvanika.example",
            ),
        ]

    monkeypatch.setattr(PulscenSource, "search", lambda self, query: fake_candidates("pulscen"))
    monkeypatch.setattr(OptlistSource, "search", lambda self, query: [])
    monkeypatch.setattr(DuckDuckGoSource, "search", lambda self, query: [])
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(
        pipeline,
        "crawl_site_text",
        lambda url, **kwargs: (
            "у нас есть цинкование и гальванические покрытия. "
            "Контакты: г. Москва, ул. Ленина, д. 5. Тел: +7 900 123-45-67, email: sales@example.ru"
        ),
    )

    companies = search_and_score("гальванические покрытия", deep_relevance=True)

    assert companies[0].contacts["phone"][0].value == "+7 900 123-45-67"
    assert companies[0].contacts["email"][0].value == "sales@example.ru"
    assert companies[0].contacts["address"][0].value == "г. Москва, ул. Ленина, д. 5"
