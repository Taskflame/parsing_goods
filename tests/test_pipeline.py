"""End-to-end прогон пайплайна без сети: сетевые вызовы источников
подменяются, чтобы протестировать normalize -> dedup -> score -> export
как единое целое (design_doc §3)."""

from datetime import date

from procurement_search.enrichment import DadataEnricher, Enricher, NullEnricher
from procurement_search.models import Candidate, Company, FieldValue, StockStatus, VerificationFlag
from procurement_search import pipeline
from procurement_search.pipeline import run_pipeline, search_and_score

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
    monkeypatch.setattr(pipeline, "classify_stock_status", lambda site_text: True)

    without_llm = search_and_score("гальванические покрытия", deep_relevance=True)
    with_llm = search_and_score(
        "гальванические покрытия", deep_relevance=True, relevance_llm_check=True
    )

    assert without_llm[0].stock_status == StockStatus.NOT_CHECKED
    assert with_llm[0].stock_status == StockStatus.OUT_OF_STOCK
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
