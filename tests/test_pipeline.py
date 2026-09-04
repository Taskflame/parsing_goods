"""End-to-end прогон пайплайна без сети: сетевые вызовы источников
подменяются, чтобы протестировать normalize -> dedup -> score -> export
как единое целое (design_doc §3)."""

from datetime import date, datetime

from procurement_search.attribute_extractor import Quantity
from procurement_search.enrichment import DadataEnricher, Enricher, NullEnricher
from procurement_search.models import (
    Availability,
    AvailabilityStatus,
    Candidate,
    Company,
    FieldValue,
    ScoreBreakdown,
    StockStatus,
    VerificationFlag,
)
from procurement_search import pipeline
from procurement_search.pipeline import run_pipeline, search_and_score
from procurement_search.quantity_match import Verdict
from procurement_search.stepper_probe import StepperProbeResult
from procurement_search.trusted_suppliers import TrustedSupplierStore

import openpyxl


class _FakeSource:
    """Подмена реального источника (build_yandex_search/build_google_cse/
    build_yandex_gen_search) — pulscen.ru/optlist.ru/DuckDuckGo убраны из
    пайплайна (design-обсуждение: селекторы Pulscen/Optlist никогда не были
    откалиброваны — 0 результатов с самого начала; DuckDuckGo упёрся в
    JS-антибот-челлендж html.duckduckgo.com, недоступный без браузера),
    поэтому тесты подменяют один из оставшихся реальных источников, а не
    несуществующий больше каталожный."""

    def __init__(self, name: str, search_fn):
        self.name = name
        self._search_fn = search_fn

    def search(self, query: str) -> list[Candidate]:
        return self._search_fn(query)


def _patch_sources(monkeypatch, search_fn, *, name: str = "yandex_search") -> None:
    """Единственный активный источник в пайплайне на время теста — один
    фейковый (build_yandex_search), остальные строители отключены (None),
    чтобы реальные YANDEX_SEARCH_API_KEY/GOOGLE_CSE_API_KEY в окружении
    (если вдруг заданы) не подмешивали настоящие сетевые источники в тест."""
    monkeypatch.setattr(pipeline, "build_yandex_search", lambda cfg: _FakeSource(name, search_fn))
    monkeypatch.setattr(pipeline, "build_google_cse", lambda cfg: None)
    monkeypatch.setattr(pipeline, "build_yandex_gen_search", lambda cfg: None)


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
    # Два РАЗНЫХ источника, оба отдают одни и те же карточки (с разным
    # source_name) — тест кросс-источникового дедупа, не одного источника.
    monkeypatch.setattr(
        pipeline, "build_yandex_search", lambda cfg: _FakeSource("yandex_search", lambda q: _fake_candidates("yandex_search"))
    )
    monkeypatch.setattr(
        pipeline, "build_google_cse", lambda cfg: _FakeSource("google_cse", lambda q: _fake_candidates("google_cse"))
    )
    monkeypatch.setattr(pipeline, "build_yandex_gen_search", lambda cfg: None)

    output = tmp_path / "out.xlsx"
    result_path = run_pipeline("гальванические покрытия", output)

    assert result_path == output
    assert output.exists()

    wb = openpyxl.load_workbook(output)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))

    # дубли по телефону/названию между двумя источниками схлопнуты в 2 компании
    assert len(rows) == 2

    names = [r[0] for r in rows]
    assert "ООО Гальванические покрытия" in names
    assert "Кондитерская фабрика Сладость" in names

    # релевантная компания (совпадение с запросом) ранжирована выше нерелевантной
    assert rows[0][0] == "ООО Гальванические покрытия"
    galvanika_score = rows[0][-1]
    candy_score = rows[1][-1]
    assert galvanika_score > candy_score


def test_brand_extraction_adds_brand_first_search_term(monkeypatch):
    """Когда бренд распознан (Слой 0, brand_extractor.py), в источники
    должен уйти отдельный запрос с брендом ПЕРВЫМ словом — не только
    сырой raw_query, где бренд стоит там же, где его написал байер."""
    queries_seen: list[str] = []

    def spy_search(query):
        queries_seen.append(query)
        return []

    _patch_sources(monkeypatch, spy_search)
    monkeypatch.setattr(pipeline, "extract_brand", lambda raw_query, **kwargs: "Пульсар")

    search_and_score(
        "частотный преобразователь пульсар 5,5 киловат 380 вольт", use_llm_fallback=True
    )

    assert any(q.startswith("Пульсар") for q in queries_seen), queries_seen


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
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Живая Компания",
                description_raw="гальванические покрытия",
            ),
            Candidate(
                source="yandex_search",
                source_url="https://x.example/2",
                name_raw="ООО Закрыто Давно",
                description_raw="гальванические покрытия",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)

    companies = search_and_score(
        "гальванические покрытия", enricher=_StatusByNameEnricher()
    )

    names = [c.name.value for c in companies]
    assert "ООО Живая Компания" in names
    assert "ООО Закрыто Давно" not in names


def test_marketplace_domain_ranked_below_non_marketplace_despite_higher_relevance(monkeypatch):
    """Демонстрирует именно то, что было в реальной выдаче: маркетплейс
    (ozon.ru) с идеальным текстовым совпадением запроса не должен обгонять
    в ранжировании обычного поставщика с чуть менее полным совпадением —
    is_marketplace_domain должен пересилить более высокий Слой-1 score."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="Дренажный насос на Ozon",
                description_raw="дренажный насос",  # полное совпадение -> максимальный Слой-1 score
                website="https://www.ozon.ru/product/nasos-drenazhnyy-123",
            ),
            Candidate(
                source="yandex_search",
                source_url="https://x.example/2",
                name_raw="ООО Насосный Завод",
                description_raw="дренажный",  # частичное совпадение -> score ниже, чем у ozon.ru
                website="https://nasos-zavod.example/",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    # is_marketplace_domain читает contacts["website"], который заполняется
    # только verify_websites=True -> _attach_website_liveness (см.
    # pipeline.py) — по умолчанию оно и так True, мокаем только саму HTTP-проверку.
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )

    companies = search_and_score("дренажный насос")

    assert companies[0].name.value == "ООО Насосный Завод"
    assert companies[1].name.value == "Дренажный насос на Ozon"
    # без демотирования маркетплейсов ozon.ru обогнал бы завод по чистому score:
    assert companies[1].score.total > companies[0].score.total


def test_website_liveness_attached_when_candidate_has_website(monkeypatch):
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="гальванические покрытия",
                website="https://galvanika.ru",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )

    companies = search_and_score("гальванические покрытия")

    assert len(companies) == 1
    website_field = companies[0].contacts["website"][0]
    assert website_field.value == "https://galvanika.ru"
    assert website_field.confidence == VerificationFlag.CONFIRMED


def test_website_liveness_skipped_when_disabled(monkeypatch):
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="гальванические покрытия",
                website="https://galvanika.ru",
            ),
        ]

    def fail_if_called(url, **kwargs):
        raise AssertionError("check_website_liveness не должен вызываться при verify_websites=False")

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(pipeline, "check_website_liveness", fail_if_called)

    companies = search_and_score("гальванические покрытия", verify_websites=False)

    assert "website" not in companies[0].contacts


def test_default_enricher_is_null_without_dadata_key(monkeypatch):
    monkeypatch.delenv("DADATA_API_KEY", raising=False)
    assert isinstance(pipeline._default_enricher(), NullEnricher)


def test_default_enricher_is_dadata_when_key_present(monkeypatch):
    monkeypatch.setenv("DADATA_API_KEY", "some-key")
    assert isinstance(pipeline._default_enricher(), DadataEnricher)


def test_stale_website_no_longer_causes_knockout(monkeypatch):
    # STALE раньше был knockout-сигналом, но на практике (design-обсуждение)
    # дал массовые ложные срабатывания — сетевые обрывы между нашей средой
    # и доменом кандидата неотличимы от "компания перестала существовать".
    # Теперь STALE только информирует байера (колонка "Сайт" в экспорте) и
    # остаётся мягким сигналом в compute_trust, но не выкидывает компанию.
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Протухший Сайт",
                description_raw="гальванические покрытия",
                website="https://dead.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.STALE
    )

    companies = search_and_score("гальванические покрытия")

    assert len(companies) == 1
    assert companies[0].name.value == "ООО Протухший Сайт"
    assert companies[0].contacts["website"][0].confidence == VerificationFlag.STALE


def test_deep_relevance_refines_score_using_crawled_site_text(monkeypatch):
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                # Название компании намеренно НЕ пересекается по смыслу с
                # запросом (в отличие от description_raw ниже) — чтобы
                # relevance до краулинга была честным нулём, и прирост от
                # Слоя 2 был виден именно как прирост от текста сайта, а
                # не от совпадения имени.
                name_raw="ООО Ромашка",
                description_raw="",  # снипет пустой — Слой 1 не найдёт ничего
                website="https://romashka.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
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
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="",
                website="https://galvanika.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(
        pipeline,
        "crawl_site_text",
        lambda url, **kwargs: "у нас есть цинкование и гальванические покрытия",
    )
    monkeypatch.setattr(pipeline, "classify_relevance", lambda raw_query, site_text: False)
    monkeypatch.setattr(pipeline, "check_attribute_match", lambda raw_query, site_text: True)

    without_llm = search_and_score("гальванические покрытия", deep_relevance=True)
    with_llm = search_and_score(
        "гальванические покрытия", deep_relevance=True, relevance_llm_check=True
    )

    assert with_llm[0].score.relevance < without_llm[0].score.relevance


def test_deep_relevance_attribute_mismatch_penalizes_score(monkeypatch):
    """check_attribute_match — отдельная проверка от classify_relevance:
    товар той же категории продаётся на сайте (is_relevant=True), но
    конкретная характеристика не совпадает с запрошенной (насос на 18 л/ч
    вместо 10000 л/час) — relevance всё равно должен просесть."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Насосы",
                description_raw="",
                website="https://nasosy.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(
        pipeline,
        "crawl_site_text",
        lambda url, **kwargs: "насос дренажный проточный 18 л/ч",
    )
    monkeypatch.setattr(pipeline, "classify_relevance", lambda raw_query, site_text: True)
    monkeypatch.setattr(pipeline, "check_attribute_match", lambda raw_query, site_text: False)

    without_llm = search_and_score("дренажный насос 10000 л/час", deep_relevance=True)
    with_llm = search_and_score(
        "дренажный насос 10000 л/час", deep_relevance=True, relevance_llm_check=True
    )

    assert with_llm[0].score.relevance < without_llm[0].score.relevance


def test_deep_relevance_llm_marks_out_of_stock_without_touching_score(monkeypatch):
    """classify_stock_status — информационная плашка (models.StockStatus),
    в отличие от classify_relevance/check_attribute_match НЕ должна менять
    score/ранжирование (design-обсуждение: "не будем включать в систему
    рейтинга пойнт про наличие товара")."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="",
                website="https://galvanika.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(
        pipeline,
        "crawl_site_text",
        lambda url, **kwargs: "у нас есть цинкование. Товар закончился.",
    )
    monkeypatch.setattr(pipeline, "classify_relevance", lambda raw_query, site_text: True)
    monkeypatch.setattr(pipeline, "check_attribute_match", lambda raw_query, site_text: True)
    monkeypatch.setattr(
        pipeline, "classify_stock_status", lambda site_text: ("out_of_stock", "Товар закончился")
    )

    without_llm = search_and_score("гальванические покрытия", deep_relevance=True)
    with_llm = search_and_score(
        "гальванические покрытия", deep_relevance=True, relevance_llm_check=True
    )

    assert without_llm[0].stock_status == StockStatus.NOT_CHECKED
    assert with_llm[0].stock_status == StockStatus.OUT_OF_STOCK
    assert with_llm[0].stock_status_quote == "Товар закончился"
    assert with_llm[0].score.relevance == without_llm[0].score.relevance


def test_attribute_mismatch_penalizes_score_without_any_llm_flag(monkeypatch):
    """scoring.has_attribute_mismatch (Слой 0.5) работает всегда, без
    --relevance-llm-check/--deep-relevance — детерминированный, дешёвый,
    для всех кандидатов сразу (design-обсуждение: та же проблема "18 л/ч
    вместо 10000 л/час", что раньше закрывалась только LLM-проверкой)."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="Насос дренажный слабый",
                description_raw="дренажный насос 18 л/ч",
            ),
            Candidate(
                source="yandex_search",
                source_url="https://x.example/2",
                name_raw="ООО Насосный Завод",
                description_raw="дренажный насос",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)

    companies = search_and_score("дренажный насос 10000 л/час", verify_websites=False)

    assert len(companies) == 2
    assert companies[0].name.value == "ООО Насосный Завод"
    assert companies[1].name.value == "Насос дренажный слабый"
    assert companies[1].score.relevance < companies[0].score.relevance


def test_relevance_llm_check_excludes_non_listing_content(monkeypatch):
    """_filter_non_listings — не требует deep_relevance=True (работает по
    сниппету, без краулинга): статья/видео с достаточным токенным
    пересечением с запросом, чтобы пройти Слой 1, должна быть жёстко
    исключена, а не просто занижена в score (design-обсуждение:
    "Как работает портативный бензиновый электрогенератор" на rutube.ru)."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="Как работает портативный бензиновый электрогенератор",
                description_raw="Разбираем принцип работы генератора FinePower",
            ),
            Candidate(
                source="yandex_search",
                source_url="https://x.example/2",
                name_raw="Бензиновый генератор FinePower FPGI-1800 купить",
                description_raw="В наличии, доставка по России",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)

    def fake_listing_type(raw_query, title, snippet):
        return "купить" in title.lower()

    monkeypatch.setattr(pipeline, "classify_listing_type", fake_listing_type)

    without_llm = search_and_score("бензогенератор FinePower FPGI-1800", verify_websites=False)
    with_llm = search_and_score(
        "бензогенератор FinePower FPGI-1800", verify_websites=False, relevance_llm_check=True
    )

    without_names = {c.name.value for c in without_llm}
    with_names = {c.name.value for c in with_llm}
    assert "Как работает портативный бензиновый электрогенератор" in without_names
    assert "Как работает портативный бензиновый электрогенератор" not in with_names
    assert "Бензиновый генератор FinePower FPGI-1800 купить" in with_names


def test_relevance_llm_check_keeps_companies_when_llm_unavailable(monkeypatch):
    """None от classify_listing_type (LLM недоступна/упала) — компания НЕ
    исключается, в отличие от False (см. docstring classify_listing_type
    в relevance_llm.py про мотивацию: не ронять байера в пустую выдачу
    из-за сетевой ошибки у LLM-провайдера)."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Тест",
                description_raw="гальванические покрытия",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(pipeline, "classify_listing_type", lambda raw_query, title, snippet: None)

    companies = search_and_score(
        "гальванические покрытия", verify_websites=False, relevance_llm_check=True
    )

    assert len(companies) == 1


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


def test_attach_site_contacts_llm_fallback_fills_only_missing_fields(monkeypatch):
    """regex находит телефон, LLM добирает email/адрес — но не переспрашивает
    телефон, который уже нашёлся (design-обсуждение: буквальное совпадение
    надёжнее генеративной догадки)."""
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )
    site_text = "Тел: +7 900 123-45-67. Пишите нам, будем рады ответить на все вопросы."

    seen_calls = []

    def fake_extract_contacts(text):
        seen_calls.append(text)
        return ("+7 999 000-00-00", "sales@example.ru", "г. Москва, ул. Тестовая, 1")

    monkeypatch.setattr(pipeline, "extract_contacts", fake_extract_contacts)

    pipeline._attach_site_contacts(company, site_text, use_llm=True)

    assert len(seen_calls) == 1
    # телефон — из regex, LLM его не перезаписала, хоть и вернула другой
    assert company.contacts["phone"][0].value == "+7 900 123-45-67"
    assert company.contacts["phone"][0].source == "текст сайта (Слой 2)"
    # email/адрес — regex не нашёл, заполнены LLM-догадкой с отдельной пометкой источника
    assert company.contacts["email"][0].value == "sales@example.ru"
    assert company.contacts["email"][0].source == "текст сайта (Слой 2, LLM)"
    assert company.contacts["address"][0].value == "г. Москва, ул. Тестовая, 1"
    assert company.contacts["address"][0].source == "текст сайта (Слой 2, LLM)"


def test_attach_site_contacts_no_llm_call_when_use_llm_false():
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )

    # use_llm=False (дефолт) — LLM не должна вызываться, даже если regex
    # ничего не нашёл; extract_contacts не подменяется, поэтому реальный
    # вызов сети/ключа сразу проявился бы ошибкой, если бы код до него дошёл.
    pipeline._attach_site_contacts(company, "Продаём генераторы 5 квт оптом и в розницу", use_llm=False)

    assert company.contacts == {}


def test_attach_site_contacts_no_llm_call_when_regex_found_everything(monkeypatch):
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )
    site_text = "Контакты: г. Москва, ул. Ленина, д. 5. Тел: +7 900 123-45-67, email: sales@example.ru"

    def fake_extract_contacts(text):
        raise AssertionError("LLM не должна вызываться, если regex уже нашёл все поля")

    monkeypatch.setattr(pipeline, "extract_contacts", fake_extract_contacts)

    pipeline._attach_site_contacts(company, site_text, use_llm=True)

    assert company.contacts["phone"][0].value == "+7 900 123-45-67"


def test_deep_relevance_populates_contacts_from_crawled_site(monkeypatch):
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Гальваник",
                description_raw="",
                website="https://galvanika.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
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


def _company_with_price(price: str | None) -> Company:
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )
    if price is not None:
        company.price = FieldValue(price, "текст сайта (Слой 2)", date.today(), VerificationFlag.UNVERIFIED)
    return company


def test_attach_site_price_extracts_from_regex():
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )

    pipeline._attach_site_price(company, "Насос дренажный. Цена: 15 000 руб.")

    assert company.price.value == "15 000 руб."
    assert company.price.source == "текст сайта (Слой 2)"
    assert company.price.confidence == VerificationFlag.UNVERIFIED


def test_attach_site_price_no_match_leaves_price_none():
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )

    pipeline._attach_site_price(company, "Продаём генераторы 5 квт оптом и в розницу")

    assert company.price is None


def test_attach_site_price_prefers_llm_even_when_regex_would_match(monkeypatch):
    """Порядок ОБРАТНЫЙ по сравнению с _attach_site_contacts (там regex
    приоритетнее LLM): на реальной карточке товара соседние числа с
    валютой (акция/похожие товары/доставка) регекс не отличает от нужной
    цены, поэтому LLM спрашивается ПЕРВОЙ и её ответ используется, даже
    если в тексте есть что-то, что PRICE_RE тоже нашёл бы."""
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )
    # PRICE_RE нашёл бы "500 руб." (первое совпадение по тексту) — не то,
    # что реально относится к запрошенному товару.
    site_text = "Похожие товары: аксессуар 500 руб. Насос дренажный, цена 15 000 руб."

    monkeypatch.setattr(pipeline, "extract_price", lambda text: "15 000 руб.")

    pipeline._attach_site_price(company, site_text, use_llm=True)

    assert company.price.value == "15 000 руб."
    assert company.price.source == "текст сайта (Слой 2, LLM)"


def test_attach_site_price_falls_back_to_regex_when_llm_finds_nothing(monkeypatch):
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )

    monkeypatch.setattr(pipeline, "extract_price", lambda text: None)

    pipeline._attach_site_price(company, "Насос дренажный, цена 15 000 руб.", use_llm=True)

    assert company.price.value == "15 000 руб."
    assert company.price.source == "текст сайта (Слой 2)"


def test_attach_site_price_no_llm_call_when_use_llm_false():
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )

    # extract_price не подменяется — реальный вызов сети сразу проявился бы
    # ошибкой, если бы код до него дошёл при use_llm=False (дефолт).
    pipeline._attach_site_price(company, "Насос дренажный в наличии", use_llm=False)

    assert company.price is None


def test_attach_site_price_rejects_zero_from_regex():
    """Реальный кейс с живой выдачи: "0 руб." на странице с JS-виджетом
    динамической цены (наш краулер не исполняет JavaScript, видит
    плейсхолдер "0" вместо настоящего значения) — ноль не более достоверная
    цена, чем её отсутствие, не должен попадать в карточку как есть."""
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )

    pipeline._attach_site_price(company, "Генератор бензиновый. Цена: 0 руб.")

    assert company.price is None


def test_attach_site_price_rejects_zero_from_llm(monkeypatch):
    company = Company(
        inn=None,
        ogrn=None,
        name=FieldValue("ООО Тест", "yandex_search", date.today(), VerificationFlag.UNVERIFIED),
        status="неизвестно",
    )
    monkeypatch.setattr(pipeline, "extract_price", lambda text: "0 руб.")

    pipeline._attach_site_price(company, "Генератор бензиновый в наличии", use_llm=True)

    # LLM вернула ноль -> отбрасываем и как последний шанс пробуем regex,
    # который на этом тексте тоже ничего не найдёт.
    assert company.price is None


def test_parse_price_value_handles_thousand_separator_and_decimals():
    assert pipeline._parse_price_value("15 000 ₽") == 15000.0
    assert pipeline._parse_price_value("1 500,50 руб.") == 1500.50
    assert pipeline._parse_price_value("25000 руб.") == 25000.0


def test_parse_price_value_returns_none_for_junk():
    assert pipeline._parse_price_value("цена по запросу") is None
    assert pipeline._parse_price_value("0 руб.") is None


def test_ranking_key_sorts_by_price_ascending_overriding_score(monkeypatch):
    """Байер явно попросил цену как отдельный жёсткий KPI ранжирования
    поверх уже готового списка: компания с более высоким score, но большей
    ценой должна оказаться НИЖЕ более дешёвой, даже менее релевантной."""
    cheap_but_less_relevant = _company_with_price("10 000 руб.")
    cheap_but_less_relevant.score = pipeline.ScoreBreakdown(
        relevance=0.3, trust=0.5, confidence=0.5, total=0.2
    )
    expensive_but_more_relevant = _company_with_price("50 000 руб.")
    expensive_but_more_relevant.score = pipeline.ScoreBreakdown(
        relevance=0.9, trust=0.5, confidence=0.5, total=0.8
    )

    companies = [expensive_but_more_relevant, cheap_but_less_relevant]
    companies.sort(key=lambda c: pipeline._ranking_key(c, marketplace_domains=[]))

    assert companies[0] is cheap_but_less_relevant
    assert companies[1] is expensive_but_more_relevant


def test_ranking_key_keeps_score_order_among_companies_without_price():
    no_price_high_score = _company_with_price(None)
    no_price_high_score.score = pipeline.ScoreBreakdown(relevance=0.9, trust=0.5, confidence=0.5, total=0.8)
    no_price_low_score = _company_with_price(None)
    no_price_low_score.score = pipeline.ScoreBreakdown(relevance=0.3, trust=0.5, confidence=0.5, total=0.2)

    companies = [no_price_low_score, no_price_high_score]
    companies.sort(key=lambda c: pipeline._ranking_key(c, marketplace_domains=[]))

    assert companies[0] is no_price_high_score
    assert companies[1] is no_price_low_score


def test_ranking_key_priced_companies_rank_above_unpriced_regardless_of_score():
    priced_low_score = _company_with_price("10 000 руб.")
    priced_low_score.score = pipeline.ScoreBreakdown(relevance=0.1, trust=0.1, confidence=0.1, total=0.05)
    unpriced_high_score = _company_with_price(None)
    unpriced_high_score.score = pipeline.ScoreBreakdown(relevance=0.9, trust=0.9, confidence=0.9, total=0.9)

    companies = [unpriced_high_score, priced_low_score]
    companies.sort(key=lambda c: pipeline._ranking_key(c, marketplace_domains=[]))

    assert companies[0] is priced_low_score
    assert companies[1] is unpriced_high_score


def test_deep_relevance_sorts_final_list_by_crawled_price(monkeypatch):
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Дорогой Насос",
                description_raw="дренажный насос",
                website="https://expensive.example",
            ),
            Candidate(
                source="yandex_search",
                source_url="https://x.example/2",
                name_raw="ООО Дешёвый Насос",
                description_raw="дренажный",
                website="https://cheap.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )

    site_texts = {
        "https://expensive.example": "дренажный насос дренажный насос, цена 90 000 руб.",
        "https://cheap.example": "дренажный насос, цена 12 000 руб.",
    }
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: site_texts[url])

    companies = search_and_score("дренажный насос", deep_relevance=True)

    # Без жёсткой сортировки по цене "Дорогой Насос" был бы первым — у него
    # выше Слой-1 relevance (описание точнее совпадает с запросом).
    assert companies[0].name.value == "ООО Дешёвый Насос"
    assert companies[1].name.value == "ООО Дорогой Насос"


class _SpyEnricher(Enricher):
    """Enricher-заглушка для юнит-тестов _attach_legal_name — просто
    записывает, с каким legal_name её вызвали, без реального резолвинга."""

    def __init__(self):
        self.re_resolve_calls: list[str] = []

    def build_company(self, candidate_group):
        raise NotImplementedError("не нужен в этих тестах — _attach_legal_name работает с готовым Company")

    def re_resolve(self, company, legal_name, candidate_group):
        self.re_resolve_calls.append(legal_name)


def _unresolved_company() -> Company:
    return Company(
        inn=None,
        ogrn=None,
        name=FieldValue(
            "Генератор бензиновый Huter DY3000L", "yandex_search", date.today(), VerificationFlag.UNVERIFIED
        ),
        status="неизвестно",
    )


def test_attach_legal_name_calls_re_resolve_when_regex_finds_name():
    company = _unresolved_company()
    enricher = _SpyEnricher()

    pipeline._attach_legal_name(company, "© 2024 ООО «Диптех». Продаём генераторы Huter.", enricher)

    assert enricher.re_resolve_calls == ["ООО «Диптех»"]


def test_attach_legal_name_no_call_when_nothing_found_and_no_llm():
    company = _unresolved_company()
    enricher = _SpyEnricher()

    pipeline._attach_legal_name(company, "Каталог товаров и услуг", enricher, use_llm=False)

    assert enricher.re_resolve_calls == []


def test_attach_legal_name_llm_fallback_when_regex_finds_nothing(monkeypatch):
    company = _unresolved_company()
    enricher = _SpyEnricher()
    monkeypatch.setattr(pipeline, "extract_legal_name", lambda text: "ООО «Диптех»")

    pipeline._attach_legal_name(company, "Каталог товаров и услуг", enricher, use_llm=True)

    assert enricher.re_resolve_calls == ["ООО «Диптех»"]


def test_attach_legal_name_skips_already_resolved_company(monkeypatch):
    company = _unresolved_company()
    company.inn = "7700000000"
    enricher = _SpyEnricher()

    def fake_extract(text):
        raise AssertionError("LLM не должна вызываться для уже резолвленной компании")

    monkeypatch.setattr(pipeline, "extract_legal_name", fake_extract)

    pipeline._attach_legal_name(company, "© 2024 ООО «Диптех»", enricher, use_llm=True)

    assert enricher.re_resolve_calls == []


def test_attach_legal_name_skips_null_enricher(monkeypatch):
    company = _unresolved_company()

    def fake_extract(text):
        raise AssertionError("LLM не должна вызываться для NullEnricher — некому использовать результат")

    monkeypatch.setattr(pipeline, "extract_legal_name", fake_extract)

    # NullEnricher.re_resolve — no-op, но проверяем именно то, что _attach_legal_name
    # даже не пытается искать название (не тратит LLM впустую), а не только
    # что результат в итоге отбрасывается.
    pipeline._attach_legal_name(company, "© 2024 ООО «Диптех»", NullEnricher(), use_llm=True)


class _FailFirstThenResolveEnricher(Enricher):
    """Имитирует DadataEnricher, которому первая попытка (заголовок
    товарной карточки вместо названия юрлица) не даёт резолвиться, а
    вторая (re_resolve с настоящим названием со Слоя 2) — даёт."""

    def __init__(self, expected_legal_name: str, resolved_status: str = "действующая"):
        self.expected_legal_name = expected_legal_name
        self.resolved_status = resolved_status

    def build_company(self, candidate_group):
        primary = candidate_group[0]
        return Company(
            inn=None,
            ogrn=None,
            name=FieldValue(
                primary.name_raw, primary.source, primary.scraped_at, VerificationFlag.UNVERIFIED
            ),
            status="неизвестно",
            sources=[primary.source],
            raw_candidates=candidate_group,
        )

    def re_resolve(self, company, legal_name, candidate_group):
        if legal_name != self.expected_legal_name:
            return
        company.inn = "7700000000"
        company.status = self.resolved_status
        company.name = FieldValue(legal_name, "ЕГРЮЛ (тест)", date.today(), VerificationFlag.CONFIRMED)


def test_deep_relevance_re_resolves_company_using_legal_name_from_site(monkeypatch):
    """Сквозной сценарий фикса: первая попытка резолвинга по заголовку
    товарной карточки не срабатывает, но Слой 2 находит на сайте реальное
    название юрлица и повторно резолвит компанию — а trust/confidence
    пересчитываются заново с учётом нового inn/статуса."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="Генератор бензиновый Huter DY3000L",
                description_raw="генератор бензиновый",
                website="https://diptec.example/product/huter-dy3000l",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(
        pipeline,
        "crawl_site_text",
        lambda url, **kwargs: "Генератор бензиновый Huter DY3000L в наличии. © 2024 ООО «Диптех».",
    )

    enricher = _FailFirstThenResolveEnricher(expected_legal_name="ООО «Диптех»")
    companies = search_and_score("генератор бензиновый", deep_relevance=True, enricher=enricher)

    assert companies[0].inn == "7700000000"
    assert companies[0].status == "действующая"
    # trust пересчитан заново (не унаследован от Слоя 1, где inn/статус
    # ещё не были известны) — статус "действующая" + резолвленный inn +
    # живой сайт должны заметно поднять trust выше нейтральной середины.
    assert companies[0].score.trust > 0.7


def test_deep_relevance_retroactively_excludes_company_resolved_as_liquidated(monkeypatch):
    """Если Слой 2 дорезолвил компанию как ликвидированную — она должна
    исчезнуть из финальной выдачи, а не остаться только потому, что уже
    прошла knockout-отсечку ДО того, как реально резолвилась (ТЗ п.4)."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="Генератор бензиновый Huter DY3000L",
                description_raw="генератор бензиновый",
                website="https://diptec.example/product/huter-dy3000l",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(
        pipeline,
        "crawl_site_text",
        lambda url, **kwargs: "Генератор бензиновый Huter DY3000L в наличии. © 2024 ООО «Диптех».",
    )

    enricher = _FailFirstThenResolveEnricher(
        expected_legal_name="ООО «Диптех»", resolved_status="ликвидирована"
    )
    companies = search_and_score("генератор бензиновый", deep_relevance=True, enricher=enricher)

    assert companies == []


def test_trusted_suppliers_disabled_by_default_no_category_call(monkeypatch):
    def fake_classify_category(*args, **kwargs):
        raise AssertionError("classify_category не должен вызываться при use_trusted_suppliers=False (дефолт)")

    monkeypatch.setattr(pipeline, "classify_category", fake_classify_category)
    _patch_sources(monkeypatch, lambda query: [])

    search_and_score("генератор бензиновый")


def test_trusted_suppliers_skipped_entirely_when_category_not_resolved(monkeypatch):
    """LLM не нашла подходящей категории (CategoryGuess.category is None) —
    работаем как обычно, глобальный поиск подключается напрямую, база
    доверенных поставщиков вообще не трогается (даже не открывается)."""
    monkeypatch.setattr(pipeline, "classify_category", lambda raw_query, categories: None)

    def fake_search(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://global.example/1",
                name_raw="Товар глобального поиска",
                description_raw="генератор бензиновый",
                website="https://global.example/1",
            )
        ]

    _patch_sources(monkeypatch, fake_search)
    monkeypatch.setattr(pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED)

    companies = search_and_score("случайный запрос без категории", use_trusted_suppliers=True)

    assert len(companies) == 1
    assert companies[0].name.value == "Товар глобального поиска"


def test_trusted_suppliers_skips_global_search_when_enough_found(monkeypatch, tmp_path):
    """Сквозной сценарий: в базе уже есть >= порога доверенных доменов для
    категории — глобальный (неограниченный) поиск не должен вызываться
    вообще, экономя квоту API на "прогретой" категории."""
    db_path = tmp_path / "trusted.db"
    with TrustedSupplierStore(db_path) as store:
        for i in range(3):
            store.record_supplier(
                domain=f"trusted{i}.example",
                name=f"ООО Доверенный {i}",
                category_code="F3",
                rank=1,
                source_query="генератор бензиновый",
            )

    monkeypatch.setattr(pipeline, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))
    monkeypatch.setattr(pipeline, "classify_category", lambda raw_query, categories: "F3")

    # Названия должны реально отличаться друг от друга, а не суффиксом-цифрой —
    # dedup.py сливает кандидатов по нечёткому сходству имени (порог 0.85),
    # "Товар с trusted0.example"/"trusted1.example" совпали бы почти целиком
    # и схлопнулись бы в одну компанию, испортив сам сценарий теста.
    names_by_domain = {
        "trusted0.example": "ООО Альфа Генератор",
        "trusted1.example": "ООО Бета Электро",
        "trusted2.example": "ЗАО Гамма Силовые Машины",
    }

    def fake_search(query: str) -> list[Candidate]:
        if not query.startswith("site:"):
            raise AssertionError(f"глобальный поиск не должен вызываться, но вызван с {query!r}")
        domain = query.split()[0].removeprefix("site:")
        return [
            Candidate(
                source="yandex_search",
                source_url=f"https://{domain}/product/1",
                name_raw=names_by_domain[domain],
                description_raw="генератор бензиновый",
                website=f"https://{domain}/product/1",
            )
        ]

    _patch_sources(monkeypatch, fake_search)
    monkeypatch.setattr(pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED)

    companies = search_and_score("генератор бензиновый", use_trusted_suppliers=True)

    names = {c.name.value for c in companies}
    assert names == set(names_by_domain.values())


def test_trusted_suppliers_falls_back_to_global_search_when_not_enough_found(monkeypatch, tmp_path):
    """В базе есть доверенные домены, но их/найденного по ним меньше
    порога — обычный глобальный поиск всё равно подключается, результаты
    объединяются, а не отбрасываются."""
    db_path = tmp_path / "trusted.db"
    with TrustedSupplierStore(db_path) as store:
        store.record_supplier(
            domain="trusted0.example",
            name="ООО Доверенный",
            category_code="F3",
            rank=1,
            source_query="генератор бензиновый",
        )

    monkeypatch.setattr(pipeline, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))
    monkeypatch.setattr(pipeline, "classify_category", lambda raw_query, categories: "F3")

    seen_queries = []

    def fake_search(query: str) -> list[Candidate]:
        seen_queries.append(query)
        if query.startswith("site:"):
            return [
                Candidate(
                    source="yandex_search",
                    source_url="https://trusted0.example/1",
                    name_raw="Товар с trusted0.example",
                    description_raw="генератор бензиновый",
                    website="https://trusted0.example/1",
                )
            ]
        return [
            Candidate(
                source="yandex_search",
                source_url="https://global.example/1",
                name_raw="Товар глобального поиска",
                description_raw="генератор бензиновый",
                website="https://global.example/1",
            )
        ]

    _patch_sources(monkeypatch, fake_search)
    monkeypatch.setattr(pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED)

    companies = search_and_score("генератор бензиновый", use_trusted_suppliers=True)

    assert any(q.startswith("site:") for q in seen_queries)
    assert any(not q.startswith("site:") for q in seen_queries)
    names = {c.name.value for c in companies}
    assert "Товар с trusted0.example" in names
    assert "Товар глобального поиска" in names


def test_trusted_suppliers_write_back_records_top_n_excluding_marketplace(monkeypatch, tmp_path):
    """После поиска top-N (не top-1) финальной выдачи должны попасть в
    базу доверенных поставщиков под резолвленной категорией —
    маркетплейс (ozon.ru) в базу не попадает, это не поставщик."""
    db_path = tmp_path / "trusted.db"
    monkeypatch.setattr(pipeline, "TrustedSupplierStore", lambda *a, **k: TrustedSupplierStore(db_path))
    monkeypatch.setattr(pipeline, "classify_category", lambda raw_query, categories: "F3")

    def fake_search(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://good1.example/1",
                name_raw="ООО Альфа Генератор",
                description_raw="генератор бензиновый",
                website="https://good1.example/1",
            ),
            Candidate(
                source="yandex_search",
                source_url="https://www.ozon.ru/product/2",
                name_raw="Генератор на Озоне",
                description_raw="генератор бензиновый",
                website="https://www.ozon.ru/product/2",
            ),
            Candidate(
                source="yandex_search",
                source_url="https://good2.example/3",
                name_raw="ЗАО Бета Силовые Машины",
                description_raw="генератор бензиновый",
                website="https://good2.example/3",
            ),
        ]

    _patch_sources(monkeypatch, fake_search)
    monkeypatch.setattr(pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED)

    search_and_score("генератор бензиновый", use_trusted_suppliers=True)

    with TrustedSupplierStore(db_path) as store:
        domains = store.domains_for_category("F3")

    assert "good1.example" in domains
    assert "good2.example" in domains
    assert "ozon.ru" not in domains


# --- check_availability (Слой 4, см. availability.py/quantity_match.py) ---


def test_order_qty_never_leaks_into_search_terms(monkeypatch):
    """Слой 0.6 (classify_roles) — order_qty ("11 шт") — количество
    ЗАКУПКИ, не характеристика товара; попадание в поисковый запрос
    зашумляло бы выдачу Yandex/Google цифрами, не относящимися к самому
    товару. Регрессия: раньше search_terms[0] всегда был raw_query целиком
    (см. pipeline.py про search_raw_query)."""
    search_calls: list[str] = []

    def fake_search(query: str) -> list[Candidate]:
        search_calls.append(query)
        return []

    _patch_sources(monkeypatch, fake_search)

    search_and_score("генератор бензиновый Hunter 11 шт", verify_websites=False)

    assert search_calls  # источник реально вызывался хотя бы раз
    for query in search_calls:
        assert "11 шт" not in query


def test_order_length_never_leaks_into_search_terms(monkeypatch):
    """Метраж закупки (order_length, "50 метров") — тот же принцип, что и
    order_qty: количество ЗАКУПКИ, не характеристика товара, не должен
    попадать в поисковый запрос."""
    search_calls: list[str] = []

    def fake_search(query: str) -> list[Candidate]:
        search_calls.append(query)
        return []

    _patch_sources(monkeypatch, fake_search)

    search_and_score("кабель ВВГ 3х2,5 500 метров", verify_websites=False)

    assert search_calls
    for query in search_calls:
        assert "500 метров" not in query


def test_condensed_kernel_injected_as_search_term_on_long_query(monkeypatch):
    """Слой 0 (query_kernel.condense_query): для длинного шаблонного запроса
    сжатое ядро ("обращение с отходами III-IV классов опасности в Чувашии")
    должно уйти в поисковый термин, чтобы поисковик понял суть без
    юридического мусора (цитаты статей, номера законов)."""
    search_calls: list[str] = []

    def fake_search(query: str) -> list[Candidate]:
        search_calls.append(query)
        return []

    # Включённый use_llm_fallback + длинный запрос -> condense_query реально
    # вызывается и возвращает ядро.
    monkeypatch.setattr(
        pipeline, "condense_query", lambda raw, use_llm_fallback=False: (
            "обращение с отходами III-IV классов опасности в Чувашии" if use_llm_fallback else None
        )
    )
    _patch_sources(monkeypatch, fake_search)

    long_query = (
        "ищу компанию в Чувашии по услугам по обращению с отходами производства и потребления "
        "III-IV классов опасности (далее — отходы), включая сбор, транспортирование. "
        "Обязательное наличие лицензии на деятельность по сбору отходов I-IV классов опасности "
    )
    search_and_score(long_query, use_llm_fallback=True, verify_websites=False)

    assert any("отходами III-IV классов опасности в Чувашии" in q for q in search_calls)


def test_no_kernel_keeps_at_most_three_search_terms(monkeypatch):
    """Без ядра (ядро = None, обычный случай) поведение не меняется: поисковых
    терминов не больше 3 (raw + бренд-термин + clean), как было до этой фичи."""
    search_calls: list[str] = []

    def fake_search(query: str) -> list[Candidate]:
        search_calls.append(query)
        return []

    monkeypatch.setattr(pipeline, "condense_query", lambda raw, use_llm_fallback=False: None)
    _patch_sources(monkeypatch, fake_search)

    # Запрос с брендом -> было бы 2 термина без чистого (clean) при бренде? На
    # деле термин clean всегда есть; проверяем только верхнюю границу 3.
    search_and_score(
        "генератор бензиновый Hunter 11 шт", use_llm_fallback=True, verify_websites=False
    )

    # Один источник; каждый термин уходит ровно одним вызовом search().
    assert len(search_calls) <= 3


def test_search_result_exposes_order_length_and_effective_amount(monkeypatch):
    def fake_candidates(query: str) -> list[Candidate]:
        return []

    _patch_sources(monkeypatch, fake_candidates)

    result = search_and_score("кабель ВВГ 3х2,5 500 метров", verify_websites=False)

    assert result.order_qty is None
    assert result.order_length == Quantity(500.0, "м", "500 метров")
    assert result.effective_order_amount() == Quantity(500.0, "м", "500 метров")


def test_check_availability_compares_order_length_against_site_meters(monkeypatch):
    """Полный сквозной путь для метража: сайт кандидата сообщает остаток в
    метрах — Слой 4 должен сравнить его именно с order_length (не
    order_qty, которого в этом запросе нет), тем же quantity_match.compare,
    что и для штучного товара."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Кабель",
                description_raw="кабель ВВГ",
                website="https://x.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: "кабель ВВГ, в наличии")

    def fake_extract_availability(product_description, site_text, source_url, units=None):
        return Availability(
            status=AvailabilityStatus.IN_STOCK_QTY,
            quantity=Quantity(300.0, "м", "300 метров"),
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url=source_url,
            checked_at=datetime.now(),
            evidence="в наличии: 300 метров",
        )

    monkeypatch.setattr(pipeline, "extract_product_availability", fake_extract_availability)

    result = search_and_score(
        "кабель ВВГ 3х2,5 500 метров", deep_relevance=True, check_availability=True
    )

    assert result[0].availability_verdict == Verdict.NOT_ENOUGH.value
    assert "300" in result[0].availability_verdict_text and "500" in result[0].availability_verdict_text


def test_summarize_availability_works_with_length_amount():
    order_length = Quantity(500.0, "м", "500 метров")

    def _company_with_length(name: str, meters: float) -> Company:
        company = Company(
            inn=None, ogrn=None, name=FieldValue(name, "test", date(2026, 1, 1)), status="неизвестно"
        )
        company.availability = Availability(
            status=AvailabilityStatus.IN_STOCK_QTY,
            quantity=Quantity(meters, "м", f"{meters:g} метров"),
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url="https://x.example",
            checked_at=datetime.now(),
            evidence=None,
        )
        return company

    companies = [_company_with_length("Поставщик А", 600.0), _company_with_length("Поставщик Б", 250.0)]

    summary = pipeline.summarize_availability(companies, order_length)

    assert summary is not None
    assert "Требуется: 500 м" in summary
    assert "850 м" in summary and "2 поставщиков" in summary
    assert "Поставщик А" in summary and "да" in summary


def test_check_availability_without_deep_relevance_is_noop(monkeypatch):
    """check_availability требует deep_relevance=True (нечего проверять без
    текста сайта top-N кандидатов) — без него флаг не должен ронять
    пайплайн, просто ничего не делает."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Тест",
                description_raw="",
            )
        ]

    _patch_sources(monkeypatch, fake_candidates)

    companies = search_and_score(
        "генератор бензиновый 11 шт", check_availability=True, verify_websites=False
    )

    assert companies[0].availability is None
    assert companies[0].availability_verdict is None


def test_check_availability_populates_fields_and_excludes_out_of_stock(monkeypatch):
    """Слой 4 заполняет company.availability/availability_verdict(_text) по
    top-N кандидатам, а поставщик с подтверждённым OUT_OF_STOCK исключается
    из финальной выдачи целиком (knockout, тот же паттерн, что у ЕГРЮЛ-
    отсечки) — в отличие от NOT_ENOUGH/UNKNOWN, которые остаются в выдаче."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://instock.example/1",
                name_raw="ООО В наличии",
                description_raw="генератор бензиновый",
                website="https://instock.example",
            ),
            Candidate(
                source="yandex_search",
                source_url="https://outofstock.example/1",
                name_raw="ООО Нет в наличии",
                description_raw="генератор бензиновый",
                website="https://outofstock.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: f"текст сайта {url}")

    def fake_extract_availability(product_description, site_text, source_url, units=None):
        if "outofstock" in source_url:
            return Availability(
                status=AvailabilityStatus.OUT_OF_STOCK,
                quantity=None,
                pack_size=None,
                min_order=None,
                lead_time_days=None,
                price=None,
                source_url=source_url,
                checked_at=datetime.now(),
                evidence="нет в наличии",
            )
        return Availability(
            status=AvailabilityStatus.IN_STOCK_QTY,
            quantity=Quantity(15.0, "шт", "15 шт"),
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url=source_url,
            checked_at=datetime.now(),
            evidence="в наличии: 15 шт",
        )

    monkeypatch.setattr(pipeline, "extract_product_availability", fake_extract_availability)

    companies = search_and_score(
        "генератор бензиновый 11 шт", deep_relevance=True, check_availability=True
    )

    assert len(companies) == 1
    assert companies[0].name.value == "ООО В наличии"
    assert companies[0].availability.quantity == Quantity(15.0, "шт", "15 шт")
    assert companies[0].availability_verdict == Verdict.ENOUGH.value
    assert "15" in companies[0].availability_verdict_text


def test_check_availability_price_fills_in_when_attach_site_price_fails(monkeypatch):
    """Регрессия по реальному кейсу с живой выдачи (prom55.ru): цена не
    найдена ни regex'ом (PRICE_RE), ни отдельным LLM-вызовом extract_price
    (relevance_llm_check=False здесь — намеренно, чтобы _attach_site_price
    гарантированно не нашёл ничего), но Слой 4 (extract_availability, свой
    отдельный LLM-вызов с той же схемой ответа) её всё-таки нашёл — эта
    цена не должна теряться, company.price обязан подхватить её как
    резерв."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Без цены в тексте",
                description_raw="смазка",
                website="https://x.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    # Текст без единого совпадения PRICE_RE — никаких чисел с валютой.
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: "смазка в наличии, отличное качество")

    def fake_extract_availability(product_description, site_text, source_url, units=None):
        return Availability(
            status=AvailabilityStatus.IN_STOCK,
            quantity=None,
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price="73 800 ₽",
            source_url=source_url,
            checked_at=datetime.now(),
            evidence="в наличии",
        )

    monkeypatch.setattr(pipeline, "extract_product_availability", fake_extract_availability)

    companies = search_and_score(
        "смазка", deep_relevance=True, relevance_llm_check=False, check_availability=True
    )

    assert companies[0].price is not None
    assert companies[0].price.value == "73 800 ₽"
    assert "Слой 4" in companies[0].price.source


# --- probe_stepper (пилот, см. stepper_probe.py) — браузер замокан, сама
# механика клика/DOM протестирована в test_stepper_probe.py (реальный
# Playwright-прогон против локальной заглушки). Здесь — только проводка
# через pipeline.py: когда пробинг запускается, как влияет на вердикт. ---


class _StubPlaywrightHandle:
    """Заглушка для (playwright_ctx, browser) — _refine_relevance безусловно
    зовёт browser.close()/playwright_ctx.stop() в finally, реальный
    Playwright здесь не нужен, сама механика клика тестируется в
    test_stepper_probe.py."""

    def close(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _fake_open_browser():
    return _StubPlaywrightHandle(), _StubPlaywrightHandle()


def test_probe_stepper_upgrades_unclear_verdict_to_enough(monkeypatch):
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Степпер",
                description_raw="смазка",
                website="https://x.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: "смазка, в наличии")
    monkeypatch.setattr(
        pipeline,
        "extract_product_availability",
        lambda product_description, site_text, source_url, units=None: Availability(
            status=AvailabilityStatus.IN_STOCK,
            quantity=None,
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url=source_url,
            checked_at=datetime.now(),
            evidence="в наличии",
        ),
    )
    monkeypatch.setattr(pipeline, "open_browser", _fake_open_browser)
    monkeypatch.setattr(
        pipeline,
        "probe_max_orderable_quantity",
        lambda url, target_qty, browser=None, product_description=None: StepperProbeResult(
            target_confirmed=True, max_orderable=None, evidence=None
        ),
    )

    companies = search_and_score(
        "смазка 5 шт", deep_relevance=True, check_availability=True, probe_stepper=True
    )

    assert companies[0].availability_verdict == Verdict.ENOUGH.value
    assert "интерактивной проверкой" in companies[0].availability_verdict_text


def test_probe_stepper_sets_not_enough_with_discovered_max(monkeypatch):
    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Степпер",
                description_raw="смазка",
                website="https://x.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: "смазка, в наличии")
    monkeypatch.setattr(
        pipeline,
        "extract_product_availability",
        lambda product_description, site_text, source_url, units=None: Availability(
            status=AvailabilityStatus.IN_STOCK,
            quantity=None,
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url=source_url,
            checked_at=datetime.now(),
            evidence="в наличии",
        ),
    )
    monkeypatch.setattr(pipeline, "open_browser", _fake_open_browser)
    monkeypatch.setattr(
        pipeline,
        "probe_max_orderable_quantity",
        lambda url, target_qty, browser=None, product_description=None: StepperProbeResult(
            target_confirmed=False, max_orderable=8.0, evidence="Доступно только 8 шт"
        ),
    )

    companies = search_and_score(
        "смазка 20 шт", deep_relevance=True, check_availability=True, probe_stepper=True
    )

    assert companies[0].availability_verdict == Verdict.NOT_ENOUGH.value
    assert "8" in companies[0].availability_verdict_text
    assert "Доступно только 8 шт" in companies[0].availability_verdict_text


def test_probe_stepper_skips_already_confident_enough_verdict(monkeypatch):
    """Пробинг не должен тратить браузер там, где Слой 4 уже дал уверенный
    ENOUGH — probe_max_orderable_quantity не должна вызываться вовсе."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Степпер",
                description_raw="смазка",
                website="https://x.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: "смазка, в наличии: 50 шт")
    monkeypatch.setattr(
        pipeline,
        "extract_product_availability",
        lambda product_description, site_text, source_url, units=None: Availability(
            status=AvailabilityStatus.IN_STOCK_QTY,
            quantity=Quantity(50.0, "шт", "50 шт"),
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url=source_url,
            checked_at=datetime.now(),
            evidence="в наличии: 50 шт",
        ),
    )
    monkeypatch.setattr(pipeline, "open_browser", _fake_open_browser)
    probe_calls = []
    monkeypatch.setattr(
        pipeline,
        "probe_max_orderable_quantity",
        lambda url, target_qty, browser=None, product_description=None: probe_calls.append(url) or StepperProbeResult(
            target_confirmed=True, max_orderable=None, evidence=None
        ),
    )

    companies = search_and_score(
        "смазка 5 шт", deep_relevance=True, check_availability=True, probe_stepper=True
    )

    assert companies[0].availability_verdict == Verdict.ENOUGH.value  # уже был ENOUGH и без пробинга
    assert probe_calls == []


def test_probe_stepper_requires_check_availability(monkeypatch):
    """probe_stepper=True без check_availability=True — no-op с
    предупреждением, open_browser не должна вызываться вовсе."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Степпер",
                description_raw="смазка",
                website="https://x.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: "смазка, в наличии")
    open_browser_calls = []
    monkeypatch.setattr(
        pipeline, "open_browser", lambda: (open_browser_calls.append(1), (object(), object()))[1]
    )

    companies = search_and_score(
        "смазка 5 шт", deep_relevance=True, check_availability=False, probe_stepper=True
    )

    assert open_browser_calls == []
    assert companies[0].availability is None


def test_probe_stepper_missing_playwright_is_noop(monkeypatch):
    """Пакет playwright не установлен (ImportError из open_browser) —
    пайплайн не падает, просто пропускает пробинг, вердикт Слоя 4 остаётся
    как есть."""

    def fake_candidates(query: str) -> list[Candidate]:
        return [
            Candidate(
                source="yandex_search",
                source_url="https://x.example/1",
                name_raw="ООО Степпер",
                description_raw="смазка",
                website="https://x.example",
            ),
        ]

    _patch_sources(monkeypatch, fake_candidates)
    monkeypatch.setattr(
        pipeline, "check_website_liveness", lambda url, **kwargs: VerificationFlag.CONFIRMED
    )
    monkeypatch.setattr(pipeline, "crawl_site_text", lambda url, **kwargs: "смазка, в наличии")
    monkeypatch.setattr(
        pipeline,
        "extract_product_availability",
        lambda product_description, site_text, source_url, units=None: Availability(
            status=AvailabilityStatus.IN_STOCK,
            quantity=None,
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url=source_url,
            checked_at=datetime.now(),
            evidence="в наличии",
        ),
    )

    def raise_import_error():
        raise ImportError("playwright не установлен")

    monkeypatch.setattr(pipeline, "open_browser", raise_import_error)

    companies = search_and_score(
        "смазка 5 шт", deep_relevance=True, check_availability=True, probe_stepper=True
    )

    # status=in_stock без числа -> IN_STOCK_NO_QTY (Слой 4), как и было —
    # пробинг не смог запуститься (playwright не установлен) и не тронул вердикт.
    assert companies[0].availability_verdict == Verdict.IN_STOCK_NO_QTY.value


def test_ranking_key_sorts_by_availability_verdict_at_equal_score(monkeypatch):
    """Внутри РОВНО одинакового score.total (и без цены) — приоритет по
    quantity_match.VERDICT_SORT_ORDER: ENOUGH выше NOT_ENOUGH выше "нет
    данных" (design-обсуждение: частичный остаток — более действенная
    зацепка для байера, чем полное отсутствие данных об остатке)."""

    def _company(name: str, verdict: Verdict | None) -> Company:
        company = Company(
            inn=None,
            ogrn=None,
            name=FieldValue(name, "test", date(2026, 1, 1)),
            status="неизвестно",
        )
        company.score = ScoreBreakdown(relevance=0.5, trust=0.5, confidence=0.5, total=0.5)
        company.availability_verdict = verdict.value if verdict else None
        return company

    companies = [
        _company("Нет данных", Verdict.UNKNOWN),
        _company("Достаточно", Verdict.ENOUGH),
        _company("Недостаточно", Verdict.NOT_ENOUGH),
    ]
    companies.sort(key=lambda c: pipeline._ranking_key(c, marketplace_domains=[]))

    assert [c.name.value for c in companies] == ["Достаточно", "Недостаточно", "Нет данных"]


def test_ranking_key_price_beats_availability_verdict(monkeypatch):
    """Явный запрос пользователя после разбора живой выдачи: цена — ГЛАВНЫЙ
    критерий сортировки, приоритет по наличию — только внутри одинаковой
    цены (см. _ranking_key). Более дешёвый кандидат с UNKNOWN должен
    обгонять более дорогого с ENOUGH — обратное уже пробовалось и было
    откачено (см. докстринг _ranking_key), потому что у большинства
    категорий товара остаток на сайте не публикуется почти никогда, и
    "приоритет наличия выше цены" на практике означал "почти всегда выше
    цены" не по содержательной причине."""

    def _company(name: str, verdict: Verdict, price_value: str, score_total: float) -> Company:
        company = Company(
            inn=None,
            ogrn=None,
            name=FieldValue(name, "test", date(2026, 1, 1)),
            status="неизвестно",
        )
        company.score = ScoreBreakdown(
            relevance=score_total, trust=0.5, confidence=0.5, total=score_total
        )
        company.availability_verdict = verdict.value
        company.price = FieldValue(price_value, "test", date(2026, 1, 1))
        return company

    cheap_but_unknown = _company("Дешёвый, но неизвестно сколько", Verdict.UNKNOWN, "218 руб.", 0.90)
    expensive_but_enough = _company("Дороже, но точно хватит", Verdict.ENOUGH, "5000 руб.", 0.30)

    companies = [expensive_but_enough, cheap_but_unknown]
    companies.sort(key=lambda c: pipeline._ranking_key(c, marketplace_domains=[]))

    assert [c.name.value for c in companies] == ["Дешёвый, но неизвестно сколько", "Дороже, но точно хватит"]


def test_ranking_key_availability_verdict_breaks_ties_within_equal_price(monkeypatch):
    """Внутри ОДИНАКОВОЙ цены (частый случай для одной и той же позиции у
    разных поставщиков) вердикт наличия всё ещё решает — цена главный
    критерий, но не единственный."""

    def _company(name: str, verdict: Verdict) -> Company:
        company = Company(
            inn=None, ogrn=None, name=FieldValue(name, "test", date(2026, 1, 1)), status="неизвестно"
        )
        company.score = ScoreBreakdown(relevance=0.5, trust=0.5, confidence=0.5, total=0.5)
        company.availability_verdict = verdict.value
        company.price = FieldValue("1000 руб.", "test", date(2026, 1, 1))
        return company

    companies = [_company("Нет данных", Verdict.UNKNOWN), _company("Достаточно", Verdict.ENOUGH)]
    companies.sort(key=lambda c: pipeline._ranking_key(c, marketplace_domains=[]))

    assert [c.name.value for c in companies] == ["Достаточно", "Нет данных"]


def test_summarize_availability_reports_totals_and_single_supplier_coverage():
    order_qty = Quantity(11.0, "шт", "11 шт")

    def _company_with_qty(name: str, qty: float) -> Company:
        company = Company(
            inn=None, ogrn=None, name=FieldValue(name, "test", date(2026, 1, 1)), status="неизвестно"
        )
        company.availability = Availability(
            status=AvailabilityStatus.IN_STOCK_QTY,
            quantity=Quantity(qty, "шт", f"{qty:g} шт"),
            pack_size=None,
            min_order=None,
            lead_time_days=None,
            price=None,
            source_url="https://x.example",
            checked_at=datetime.now(),
            evidence=None,
        )
        return company

    companies = [_company_with_qty("Компания А", 15.0), _company_with_qty("Компания Б", 5.0)]

    summary = pipeline.summarize_availability(companies, order_qty)

    assert summary is not None
    assert "Требуется: 11 шт" in summary
    assert "20 шт" in summary and "2 поставщиков" in summary
    assert "Компания А" in summary and "да" in summary


def test_summarize_availability_returns_none_without_order_qty():
    assert pipeline.summarize_availability([], None) is None
