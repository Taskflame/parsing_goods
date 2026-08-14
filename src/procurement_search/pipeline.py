"""Оркестрация полного пайплайна (design_doc §3): [1]-[7] в одном вызове.

`search_and_score` и `run_pipeline` разделены, чтобы вызывающий код (CLI,
веб-API) мог получить список Company как данные — не только как файл на
диске. `run_pipeline` — тонкая обёртка для CLI-сценария "запрос -> Excel".
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

from procurement_search.config import load_categories, load_scoring_weights, load_sources_config
from procurement_search.dedup import dedup_candidates
from procurement_search.enrichment import DadataEnricher, Enricher, NullEnricher
from procurement_search.export import export_companies_to_excel
from procurement_search.models import Candidate, Company, FieldValue, ScoreBreakdown, VerificationFlag
from procurement_search.query_normalizer import normalize_query
from procurement_search.relevance_llm import classify_relevance
from procurement_search.scoring import combine_score, compute_score, query_tokens, weights_for_category
from procurement_search.site_relevance import compute_site_relevance, crawl_site_text
from procurement_search.sources.base import ADDRESS_RE, EMAIL_RE, PHONE_RE, first_match
from procurement_search.sources.duckduckgo import build_default as build_duckduckgo
from procurement_search.sources.google_cse import build_default as build_google_cse
from procurement_search.sources.optlist import build_default as build_optlist
from procurement_search.sources.pulscen import build_default as build_pulscen
from procurement_search.sources.yandex_search import build_default as build_yandex_search
from procurement_search.verify_contacts import check_website_liveness

logger = logging.getLogger(__name__)

# Статусы, при которых компания считается прекратившей существование
# (ТЗ, п.4 "Отсев неактуальных данных: исключать прекративших
# существование") — исключается из финальной выдачи целиком, а не просто
# занижается в скоринге (knockout, а не штраф баллами — иначе крупная
# старая ликвидированная компания всё равно может обогнать маленького
# живого поставщика). Заполняется реальными данными только реальным
# Enricher'ом (см. enrichment.DadataEnricher); NullEnricher всегда отдаёт
# "неизвестно", так что без резолвинга в ЕГРЮЛ фильтр не сработает ни на
# ком — честно, так как без резолвинга у нас нет оснований считать
# компанию мёртвой.
DEAD_COMPANY_STATUSES = {"ликвидирована"}

# Ключи, при которых Слой 3 (LLM) снижает relevance, а не обнуляет её —
# один неверный вердикт модели не должен полностью убить кандидата,
# которого Слои 1-2 честно нашли по реальному тексту сайта.
_LLM_NEGATIVE_RELEVANCE_MULTIPLIER = 0.3


def _is_knockout(company: Company) -> bool:
    """Жёсткая отсечка ДО скоринга (design-обсуждение весов: "это не баллы,
    это бинарное «выкинуть»") — в отличие от заниженного score, гарантирует,
    что такие компании не всплывут в выдаче ни при каком раскладе весов.

    Сейчас закрыты два сигнала, для которых реально есть данные:
      - статус ЕГРЮЛ = "ликвидирована";
      - сайт компании технически не отвечает (обрыв соединения/DNS, а не
        просто код ошибки — см. verify_contacts.py про то, почему это
        строгий критерий, не путается с антибот-блоком).

    Не реализовано за отсутствием источника данных: недостоверность
    сведений в ЕГРЮЛ, реестр недобросовестных поставщиков (РНП),
    дисквалифицированный руководитель, массовый адрес регистрации —
    появятся, когда будут подключены соответствующие реестры."""
    if company.status in DEAD_COMPANY_STATUSES:
        return True
    website_flags = [fv.confidence for fv in company.contacts.get("website", [])]
    if VerificationFlag.STALE in website_flags:
        return True
    return False


def _source_name(source) -> str:
    """Имя источника для логов — большинство источников (CatalogSource,
    DuckDuckGoSource) держат его в source.config.name (SourceConfig из
    config/sources.yaml); API-источники без CSS-селекторов (GoogleCseSource,
    YandexSearchSource) не нуждаются в полном SourceConfig и хранят имя
    напрямую в source.name — эта функция сглаживает разницу."""
    config = getattr(source, "config", None)
    return config.name if config is not None else source.name


def _default_enricher() -> Enricher:
    """DadataEnricher, если задан DADATA_API_KEY, иначе честная заглушка
    NullEnricher. Тот же паттерн, что у LLM_PROVIDER/ANTHROPIC_API_KEY —
    фича включается наличием переменной окружения, без правки кода."""
    api_key = os.environ.get("DADATA_API_KEY")
    if api_key:
        return DadataEnricher(api_key=api_key)
    return NullEnricher()


def search_and_score(
    raw_query: str,
    enricher: Enricher | None = None,
    use_llm_fallback: bool = False,
    verify_websites: bool = True,
    deep_relevance: bool = False,
    relevance_llm_check: bool = False,
    deep_relevance_top_n: int = 20,
) -> list[Company]:
    """Шаги [1]-[6]: нормализация -> кандидаты -> дедуп -> обогащение ->
    knockout -> скоринг -> (опционально) уточнение релевантности.

    Без явно переданного `enricher` и без `DADATA_API_KEY` в окружении
    используется NullEnricher (без резолвинга в ЕГРЮЛ) — см. enrichment.py
    про то, почему это заглушка и что нужно для боевого режима.

    verify_websites=True по умолчанию: бесплатная HEAD-проверка сайта
    каждой компании (verify_contacts.py, ТЗ п.4 "контроль актуальности
    контактов") — не требует ключей, но добавляет один HTTP-запрос на
    компанию с таймаутом до 5с. Отключайте для скорости в тестах/офлайн.
    Выполняется ДО knockout и скоринга — иначе Trust не видел бы флаг
    "сайт жив/протух", а knockout не мог бы отсеять компании с мёртвым
    сайтом.

    deep_relevance=False по умолчанию: Слой 2 (краулинг сайта top-N
    кандидатов + пересчёт relevance по реальному тексту, см.
    site_relevance.py) — не бесплатно по времени (HTTP-запросы на каждого
    из `deep_relevance_top_n` кандидатов), поэтому опционально.

    relevance_llm_check=False по умолчанию: Слой 3 (точечная LLM-проверка
    поверх Слоя 2, см. relevance_llm.py) — требует LLM_PROVIDER
    (anthropic/ollama) и не действует без deep_relevance=True (нечего
    проверять без текста сайта).
    """
    enricher = enricher or _default_enricher()

    categories = load_categories()
    sources_cfg = load_sources_config()
    weights_cfg = load_scoring_weights()

    normalized = normalize_query(raw_query, categories=categories, use_llm_fallback=use_llm_fallback)
    logger.info(
        "Нормализованный запрос: категория=%s, терминов=%d",
        normalized.category,
        len(normalized.search_terms),
    )

    candidates: list[Candidate] = []
    sources = [build_pulscen(sources_cfg), build_optlist(sources_cfg), build_duckduckgo(sources_cfg)]
    google_cse = build_google_cse(sources_cfg)
    if google_cse is not None:
        sources.append(google_cse)
    else:
        logger.info(
            "GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX не заданы — источник глобального "
            "поиска (Google CSE, охватывает и зарубежные площадки типа Alibaba) отключён"
        )
    yandex_search = build_yandex_search(sources_cfg)
    if yandex_search is not None:
        sources.append(yandex_search)
    else:
        logger.info(
            "YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID не заданы — источник "
            "глобального поиска (Yandex Search API) отключён"
        )
    for source in sources:
        for term in normalized.search_terms[:3]:  # первые несколько терминов, не весь список синонимов
            found = source.search(term)
            logger.info("%s: %d кандидатов по запросу '%s'", _source_name(source), len(found), term)
            candidates.extend(found)

    if not candidates:
        logger.warning(
            "Кандидатов не найдено — либо источники недоступны из этой сети, "
            "либо селекторы в config/sources.yaml не откалиброваны (см. README.md)."
        )

    groups = dedup_candidates(candidates)
    logger.info("После дедупликации: %d уникальных компаний из %d кандидатов", len(groups), len(candidates))

    companies: list[Company] = []
    for group in groups:
        company = enricher.build_company(group)
        if verify_websites:
            _attach_website_liveness(company, group)
        companies.append(company)

    alive_companies = [c for c in companies if not _is_knockout(c)]
    excluded_count = len(companies) - len(alive_companies)
    if excluded_count:
        logger.info(
            "Исключено %d компаний на knockout-отсечке (ликвидированы или сайт технически "
            "не отвечает) — ТЗ: 'Отсев неактуальных данных'",
            excluded_count,
        )

    for company in alive_companies:
        company.score = compute_score(company, normalized, weights=weights_cfg)
    alive_companies.sort(key=lambda c: c.score.total if c.score else 0.0, reverse=True)

    if deep_relevance:
        _refine_relevance(
            alive_companies[:deep_relevance_top_n],
            raw_query=normalized.raw_query,
            tokens=query_tokens(normalized),
            weights=weights_for_category(normalized.category, weights_cfg),
            use_llm=relevance_llm_check,
        )
        alive_companies.sort(key=lambda c: c.score.total if c.score else 0.0, reverse=True)

    return alive_companies


def _refine_relevance(
    companies: list[Company],
    raw_query: str,
    tokens: set[str],
    weights: dict,
    use_llm: bool,
) -> None:
    """Слои 2-3 уточнения релевантности (design-обсуждение скоринга) — на
    входе уже отранжированный по грубому Слою-1-скору срез top-N, не вся
    выдача (краулинг+LLM на 100-200 кандидатов не оправданы по времени/
    стоимости). Мутирует company.score на месте.

    Если у кандидата нет сайта или он не отдался — relevance не трогается,
    остаётся значением Слоя 1 (нет данных для уточнения — не выдумываем)."""
    for company in companies:
        website_entries = company.contacts.get("website", [])
        if not website_entries:
            continue

        site_text = crawl_site_text(website_entries[0].value)
        if site_text is None:
            continue

        _attach_site_contacts(company, site_text)

        relevance = compute_site_relevance(tokens, site_text)

        if use_llm:
            verdict = classify_relevance(raw_query, site_text)
            if verdict is False:
                relevance *= _LLM_NEGATIVE_RELEVANCE_MULTIPLIER

        # company.score всегда заполнен на этом шаге — _refine_relevance
        # вызывается только после того, как compute_score прошёл по всем
        # alive_companies (см. search_and_score).
        trust = company.score.trust
        confidence = company.score.confidence
        total = combine_score(relevance, trust, confidence, weights)
        company.score = ScoreBreakdown(
            relevance=relevance, trust=trust, confidence=confidence, total=total
        )


def _attach_website_liveness(company: Company, candidate_group: list[Candidate]) -> None:
    """Добавляет в contacts["website"] результат HEAD-проверки сайта компании
    (ТЗ п.4 "контроль актуальности контактов", verify_contacts.py). Сайт
    берётся из кандидатов группы (Candidate.website — собственный сайт
    компании, не профиль на pulscen/optlist, см. models.Candidate). Если ни
    у одного кандидата в группе нет website — молча пропускаем: нечего
    проверять, не выдумываем URL."""
    website_url = next((c.website for c in candidate_group if c.website), None)
    if website_url is None:
        return
    confidence = check_website_liveness(website_url)
    company.contacts.setdefault("website", []).append(
        FieldValue(website_url, "проверка доступности сайта", date.today(), confidence)
    )


def _attach_site_contacts(company: Company, site_text: str) -> None:
    """Дополняет contacts телефоном/email/адресом, найденными в тексте
    сайта, который Слой 2 уже скачал для уточнения relevance
    (crawl_site_text, обычно включая /kontakty) — переиспользуем уже
    сделанный HTTP-запрос, не ходим за контактами отдельно. Реальная
    страница контактов почти всегда информативнее короткого 1-2-
    предложенческого сниппета поисковика, из которого email/адрес
    обычно и не извлечь (см. sources/yandex_search.py).

    Только UNVERIFIED, как и остальные контакты со скрапинга — regex по
    тексту сайта не заменяет независимую проверку (MX/SMTP, см.
    verify_contacts.py про честную границу того, что здесь реализовано).

    insert(0, ...), а не append: export.py/webapp.py показывают только
    ПЕРВОЕ значение поля — контакт с реального сайта компании надёжнее
    угаданного регексом из короткого сниппета поисковика (см. docstring
    класса), должен иметь приоритет при отображении, если оба нашлись."""
    today = date.today()
    for field_name, pattern in (("phone", PHONE_RE), ("email", EMAIL_RE), ("address", ADDRESS_RE)):
        match = first_match(pattern, site_text)
        if match:
            company.contacts.setdefault(field_name, []).insert(
                0, FieldValue(match, "текст сайта (Слой 2)", today, VerificationFlag.UNVERIFIED)
            )


def run_pipeline(
    raw_query: str,
    output_path: str | Path,
    enricher: Enricher | None = None,
    use_llm_fallback: bool = False,
    verify_websites: bool = True,
    deep_relevance: bool = False,
    relevance_llm_check: bool = False,
) -> Path:
    """Прогоняет запрос через весь пайплайн и пишет результат в Excel (CLI-сценарий)."""
    companies = search_and_score(
        raw_query,
        enricher=enricher,
        use_llm_fallback=use_llm_fallback,
        verify_websites=verify_websites,
        deep_relevance=deep_relevance,
        relevance_llm_check=relevance_llm_check,
    )
    return export_companies_to_excel(companies, output_path)
