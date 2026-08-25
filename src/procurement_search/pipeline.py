"""Оркестрация полного пайплайна (design_doc §3): [1]-[7] в одном вызове.

`search_and_score` и `run_pipeline` разделены, чтобы вызывающий код (CLI,
веб-API) мог получить список Company как данные — не только как файл на
диске. `run_pipeline` — тонкая обёртка для CLI-сценария "запрос -> Excel".
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from pathlib import Path

from procurement_search.attribute_extractor import extract_attributes
from procurement_search.brand_extractor import extract_brand
from procurement_search.config import (
    load_marketplace_domains,
    load_scoring_weights,
    load_sources_config,
    load_units,
)
from procurement_search.dedup import dedup_candidates
from procurement_search.enrichment import DadataEnricher, Enricher, NullEnricher
from procurement_search.export import export_companies_to_excel
from procurement_search.models import (
    Candidate,
    Company,
    FieldValue,
    ScoreBreakdown,
    StockStatus,
    VerificationFlag,
)
from procurement_search.query_normalizer import normalize_query
from procurement_search.relevance_llm import (
    check_attribute_match,
    classify_listing_type,
    classify_relevance,
    classify_stock_status,
    extract_contacts,
)
from procurement_search.scoring import (
    combine_score,
    compute_score,
    has_attribute_mismatch,
    is_marketplace_domain,
    query_tokens,
    weights_for_category,
)
from procurement_search.site_relevance import compute_site_relevance, crawl_site_text
from procurement_search.sources.base import ADDRESS_RE, EMAIL_RE, PHONE_RE, first_match
from procurement_search.sources.google_cse import build_default as build_google_cse
from procurement_search.sources.yandex_gen_search import build_default as build_yandex_gen_search
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

# Тот же принцип, что у _LLM_NEGATIVE_RELEVANCE_MULTIPLIER, но для
# scoring.has_attribute_mismatch (Слой 0.5, детерминированный, без LLM) —
# отдельная константа, не переиспользует LLM-множитель напрямую: scoring.py
# не должен зависеть от pipeline.py, а совпадающее значение — просто
# намеренная симметрия, не связанность кода.
_ATTRIBUTE_MISMATCH_RELEVANCE_MULTIPLIER = 0.3


def _ranking_key(company: Company, marketplace_domains: list[str]) -> tuple[bool, float]:
    """Двухуровневая сортировка (по возрастанию): сначала не-маркетплейсы,
    внутри каждой группы — по убыванию score.total (см. is_marketplace_domain
    в scoring.py про мотивацию). Маркетплейсы не выкидываются из выдачи, а
    гарантированно оказываются НИЖЕ любого не-маркетплейса независимо от их
    score — всплывают в видимом топе только когда конкурентов не хватает,
    а не когда у них случайно высокий score.

    Применяется ДО среза `alive_companies[:deep_relevance_top_n]` в
    search_and_score — значит и бюджет краулинга Слоя 2/3 не тратится на
    маркетплейсы, пока есть чем его заполнить без них."""
    score = company.score.total if company.score else 0.0
    return (is_marketplace_domain(company, marketplace_domains), -score)


def _is_knockout(company: Company) -> bool:
    """Жёсткая отсечка ДО скоринга (design-обсуждение весов: "это не баллы,
    это бинарное «выкинуть»") — в отличие от заниженного score, гарантирует,
    что такие компании не всплывут в выдаче ни при каком раскладе весов.

    Сейчас закрыт один сигнал: статус ЕГРЮЛ = "ликвидирована" — авторитетный
    источник (Dadata/ФНС), не подверженный сетевым помехам между нашим
    сервером и сайтом кандидата.

    STALE (сайт не ответил на HEAD-запрос) сюда сознательно НЕ включён —
    раньше включался, но на практике (design-обсуждение) дал массовые
    ложные срабатывания: живые сайты реальных дилеров (irbismotors.ru,
    darexmoto.ru и т.п.) помечались "протух" из-за сетевых обрывов
    (SSL EOF, таймауты) между нашей средой и их доменами, а не потому что
    компания прекратила существование — HEAD-таймаут не отличает "сайта
    нет" от "наша сеть не достаёт до этого конкретного домена". Жёстко
    выкидывать по такому шумному сигналу опаснее, чем показать байеру
    неактуальный сайт с пометкой "протух" в колонке (см. export.py) —
    он остаётся видимым сигналом качества (compute_trust,
    website_alive), просто не бинарным выкидыванием. Знак жизни сайта
    всё ещё пишется в contacts["website"] (_attach_website_liveness) для
    информации байеру и как мягкий сигнал trust — не теряется, просто
    больше не решает единолично, попадёт ли компания в выдачу.

    Не реализовано за отсутствием источника данных: недостоверность
    сведений в ЕГРЮЛ, реестр недобросовестных поставщиков (РНП),
    дисквалифицированный руководитель, массовый адрес регистрации —
    появятся, когда будут подключены соответствующие реестры."""
    return company.status in DEAD_COMPANY_STATUSES


def _filter_non_listings(companies: list[Company], raw_query: str) -> list[Company]:
    """LLM-фильтр по заголовку+сниппету (design-обсуждение: в выдаче
    попадались статьи/видео/обзоры — "Как работает портативный генератор"
    на rutube.ru, "Белый список производителей" на блоге — у которых
    достаточно токенного пересечения с запросом, чтобы пройти Слой 1, но
    которые не являются страницей, где товар можно купить). В отличие от
    маркетплейсов (is_marketplace_domain, scoring.py) — не топится в
    приоритете, а ЖЁСТКО исключается: у статьи/видео нет ни оффера, ни
    контактов компании, доставать оттуда нечего, в отличие от
    маркетплейса, который теоретически мог бы быть местом покупки.

    Работает по сниппету из поисковой выдачи, не по краулингу сайта —
    поэтому, в отличие от Слоя 2/3 (site_relevance.py, _refine_relevance),
    не требует deep_relevance=True и применяется здесь ко ВСЕМ выжившим
    после ЕГРЮЛ-knockout кандидатам, а не только к top-N.

    False от classify_listing_type — исключаем. None (LLM недоступна/
    упала) — оставляем: не хотим ронять байера в пустую выдачу из-за
    сетевой ошибки у LLM-провайдера (тот же принцип, что у check_attribute_match)."""
    kept: list[Company] = []
    for company in companies:
        primary = company.raw_candidates[0] if company.raw_candidates else None
        snippet = primary.description_raw if primary else None
        verdict = classify_listing_type(raw_query, company.name.value, snippet)
        if verdict is False:
            continue
        kept.append(company)
    return kept


def _default_enricher() -> Enricher:
    """DadataEnricher, если задан DADATA_API_KEY, иначе честная заглушка
    NullEnricher. Тот же паттерн, что у LLM_PROVIDER/YANDEX_FM_API_KEY —
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

    Слой 0.5 (scoring.has_attribute_mismatch) работает всегда, без флагов:
    числовые атрибуты запроса ("10000 л/час") извлекаются
    attribute_extractor.extract_attributes и сравниваются с тем же самым
    экстрактором на тексте кандидата (без LLM — дёшево, для всех
    кандидатов сразу). Явное расхождение в разы у совпадающей единицы
    измерения занижает relevance (не выкидывает компанию) — та же задача,
    что у relevance_llm.check_attribute_match (Слой 3), но бесплатная,
    грубее и работает на всей выдаче, а не только top-N.

    relevance_llm_check=False по умолчанию: требует переменных окружения
    выбранного провайдера LLM_PROVIDER (yandexgpt/cloudru). Включает два
    независимых LLM-фильтра:
      - _filter_non_listings (по сниппету, без краулинга) — исключает
        статьи/видео/обзоры, прошедшие Слой 1 по токенам, но не
        являющиеся страницей с товаром; работает всегда, deep_relevance
        не нужен;
      - Слой 3 (classify_relevance/check_attribute_match поверх Слоя 2,
        см. relevance_llm.py) — требует deep_relevance=True (нечего
        проверять без текста сайта), без него не действует.
      - classify_stock_status (тоже поверх Слоя 2, requires deep_relevance=True) —
        ищет на сайте явную плашку "нет в наличии"/"товар закончился" и пишет
        результат в company.stock_status (models.StockStatus). В отличие от
        двух пунктов выше — НЕ влияет на score/ранжирование, это только
        информационная плашка для байера (см. export.py/webapp.py).
    """
    enricher = enricher or _default_enricher()

    sources_cfg = load_sources_config()
    weights_cfg = load_scoring_weights()
    marketplace_domains = load_marketplace_domains()

    normalized = normalize_query(raw_query)

    # Слой 0.5: числовые атрибуты запроса ("10000 л/час") — используются
    # ниже в scoring.has_attribute_mismatch, чтобы занизить relevance
    # кандидатов с явно другим значением того же параметра. use_llm_fallback
    # тратит LLM только на "голые" числа в самом запросе, один раз, не на
    # каждого кандидата.
    #
    # clean_text (тот же вызов, раньше выбрасывался) используется дважды:
    #   1. Слой 1/2 (query_tokens/compute_relevance/compute_site_relevance) —
    #      вместо сырого raw_query при сравнении токенов: числа и слова
    #      единиц измерения не должны сравниваться как обычные токены,
    #      это влияет только на РАНЖИРОВАНИЕ уже найденных кандидатов.
    #   2. Поисковые термины ниже (search_terms) — короткий "чистый" запрос
    #      как ОТДЕЛЬНАЯ попытка поиска, наравне с raw_query. Это влияет на
    #      то, какие кандидаты вообще НАЙДУТСЯ: длинный запрос со всеми
    #      цифрами топит бренд/название уже в ранжировании самого внешнего
    #      источника (Yandex/DDG/каталог), не только в нашем — короткий
    #      запрос даёт источнику отдельный шанс найти то же самое иначе.
    # Числовое соответствие само по себе проверяется отдельно, в
    # has_attribute_mismatch — по смыслу значения, а не по совпадению цифр.
    units_cfg = load_units()
    query_extraction = extract_attributes(
        normalized.raw_query, units=units_cfg, use_llm_fallback=use_llm_fallback
    )
    query_attributes = query_extraction.attributes
    clean_query_text = query_extraction.clean_text

    # Бренд (brand_extractor.py) — открытый список, нет словарного пути,
    # только LLM, тот же флаг use_llm_fallback. Используется ниже как
    # приоритетный поисковый термин с брендом ПЕРВЫМ словом — в отличие
    # от clean_query_text, где бренд стоит там же, где его написал байер
    # (обычно не в начале). Не привязано к конкретному бренду/примеру —
    # работает для любого запроса, где LLM распознала имя производителя.
    brand = extract_brand(normalized.raw_query, use_llm_fallback=use_llm_fallback)

    candidates: list[Candidate] = []
    # pulscen.ru/optlist.ru (CatalogSource, CSS-селекторы) и DuckDuckGo
    # убраны из активных источников (design-обсуждение): у первых двух
    # селекторы так и остались нужны никогда откалиброванными PLACEHOLDER'ами
    # с самого начала — 0 кандидатов; DuckDuckGo упёрся в JS-антибот-
    # челлендж на html.duckduckgo.com (anomaly.js), недоступный без
    # исполнения JavaScript, которого у нас нет. Модули/CatalogSource
    # удалены, а не просто отключены — см. git-историю, если понадобится
    # вернуть при появлении рабочей замены (например, обход через браузер).
    sources = []
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
    yandex_gen_search = build_yandex_gen_search(sources_cfg)
    if yandex_gen_search is not None:
        sources.append(yandex_gen_search)
        logger.info(
            "Yandex gen-search включён (YANDEX_GEN_SEARCH_ENABLED=true) — платно, "
            "заметно дороже классического yandex_search, см. sources/yandex_gen_search.py"
        )
    # Дополнительные поисковые термины, сразу после raw_query. Раньше во
    # внешние источники уходил ТОЛЬКО raw_query целиком, и длинный запрос
    # со всеми характеристиками топит бренд/название в собственном
    # ранжировании источника не хуже, чем топил его наш Layer 1 (design-
    # обсуждение: конкретный кейс, где бренд не находился на первых
    # позициях выдачи именно из-за длины и зашумлённости запроса).
    #   1. brand_first_term (если бренд распознан) — короткий запрос с
    #      брендом ПЕРВЫМ словом, а не там, где его написал байер.
    #   2. clean_query_text — короткий запрос "название товара" без чисел/
    #      единиц измерения вообще (с брендом на исходном месте).
    extra_terms = [clean_query_text]
    if brand:
        without_brand = re.sub(re.escape(brand), "", clean_query_text, flags=re.IGNORECASE)
        without_brand = re.sub(r"\s+", " ", without_brand).strip()
        brand_first_term = f"{brand} {without_brand}".strip() if without_brand else brand
        extra_terms.insert(0, brand_first_term)
    search_terms = list(dict.fromkeys([normalized.raw_query, *extra_terms]))
    for source in sources:
        for term in search_terms[:3]:  # raw_query + бренд-термин + clean_text, не больше
            found = source.search(term)
            logger.info("%s: %d кандидатов по запросу '%s'", source.name, len(found), term)
            candidates.extend(found)

    if not candidates:
        logger.warning(
            "Кандидатов не найдено — ни один источник (Google CSE/Yandex Search/"
            "Yandex gen-search) не настроен или недоступен из этой сети (см. README.md)."
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

    if relevance_llm_check:
        before_count = len(alive_companies)
        alive_companies = _filter_non_listings(alive_companies, normalized.raw_query)
        filtered_count = before_count - len(alive_companies)
        if filtered_count:
            logger.info(
                "Исключено %d компаний LLM-фильтром типа контента (статья/видео/обзор, "
                "не карточка товара)",
                filtered_count,
            )

    weights_for_query = weights_for_category(normalized.category, weights_cfg)
    mismatch_count = 0
    for company in alive_companies:
        company.score = compute_score(
            company, normalized, weights=weights_cfg, clean_query_text=clean_query_text
        )
        if has_attribute_mismatch(company, query_attributes, units=units_cfg):
            mismatch_count += 1
            relevance = company.score.relevance * _ATTRIBUTE_MISMATCH_RELEVANCE_MULTIPLIER
            company.score = ScoreBreakdown(
                relevance=relevance,
                trust=company.score.trust,
                confidence=company.score.confidence,
                total=combine_score(relevance, company.score.trust, company.score.confidence, weights_for_query),
            )
    if mismatch_count:
        logger.info(
            "Занижен score у %d компаний — числовой атрибут запроса (Слой 0.5, без LLM) "
            "явно не совпадает с сайтом кандидата",
            mismatch_count,
        )
    alive_companies.sort(key=lambda c: _ranking_key(c, marketplace_domains))

    if deep_relevance:
        _refine_relevance(
            alive_companies[:deep_relevance_top_n],
            raw_query=normalized.raw_query,
            tokens=query_tokens(normalized, clean_query_text=clean_query_text),
            weights=weights_for_query,
            use_llm=relevance_llm_check,
        )
        alive_companies.sort(key=lambda c: _ranking_key(c, marketplace_domains))

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
    no_website = 0
    crawl_failed = 0
    for company in companies:
        website_entries = company.contacts.get("website", [])
        if not website_entries:
            no_website += 1
            continue

        site_text = crawl_site_text(website_entries[0].value)
        if site_text is None:
            crawl_failed += 1
            logger.info(
                "Слой 2: сайт %r не отдал текст (недоступен/пусто/запрещён robots.txt) — "
                "Слои 2-3 (в т.ч. наличие товара) для %r пропущены",
                website_entries[0].value,
                company.name.value,
            )
            continue

        _attach_site_contacts(company, site_text, use_llm=use_llm)

        relevance = compute_site_relevance(tokens, site_text)

        if use_llm:
            verdict = classify_relevance(raw_query, site_text)
            if verdict is False:
                relevance *= _LLM_NEGATIVE_RELEVANCE_MULTIPLIER

            # Отдельная проверка — не "продаётся ли товар вообще", а
            # "совпадают ли его характеристики" (design-обсуждение: запрос
            # "насос 10000 л/час" не должен матчиться с найденным на
            # 18 л/ч, даже если это тот же класс товара и Слой 3 выше уже
            # подтвердил relevance). Считается независимо от verdict —
            # relevant, но с несовпадающим параметром, должен просесть
            # так же, как совсем нерелевантный кандидат.
            match_verdict = check_attribute_match(raw_query, site_text)
            if match_verdict is False:
                relevance *= _LLM_NEGATIVE_RELEVANCE_MULTIPLIER

            # Наличие товара (models.StockStatus) — намеренно НЕ участвует в
            # relevance/score (design-обсуждение: это информационная плашка
            # для байера в духе verify_contacts.check_website_liveness,
            # "сайт жив/протух", а не сигнал качества поставщика — статус
            # наличия конкретной позиции может устареть быстрее, чем байер
            # успеет посмотреть отчёт, и жёстко штрафовать/выкидывать
            # поставщика по нему было бы неоправданно).
            stock_verdict = classify_stock_status(site_text)
            if stock_verdict is True:
                company.stock_status = StockStatus.OUT_OF_STOCK
            elif stock_verdict is False:
                company.stock_status = StockStatus.IN_STOCK
            # None (LLM недоступна/упала) — stock_status остаётся
            # NOT_CHECKED, не выдумываем результат.

        # company.score всегда заполнен на этом шаге — _refine_relevance
        # вызывается только после того, как compute_score прошёл по всем
        # alive_companies (см. search_and_score).
        trust = company.score.trust
        confidence = company.score.confidence
        total = combine_score(relevance, trust, confidence, weights)
        company.score = ScoreBreakdown(
            relevance=relevance, trust=trust, confidence=confidence, total=total
        )

    if no_website or crawl_failed:
        logger.info(
            "Слой 2/3 пропущен у %d из %d кандидатов (нет сайта: %d, сайт не отдал текст: %d) — "
            "у них relevance/наличие товара остались как после Слоя 1, без уточнения",
            no_website + crawl_failed,
            len(companies),
            no_website,
            crawl_failed,
        )


def _attach_website_liveness(company: Company, candidate_group: list[Candidate]) -> None:
    """Добавляет в contacts["website"] результат HEAD-проверки сайта компании
    (ТЗ п.4 "контроль актуальности контактов", verify_contacts.py). Сайт
    берётся из кандидатов группы (Candidate.website — собственный сайт
    компании, не страница листинга источника, см. models.Candidate). Если ни
    у одного кандидата в группе нет website — молча пропускаем: нечего
    проверять, не выдумываем URL."""
    website_url = next((c.website for c in candidate_group if c.website), None)
    if website_url is None:
        return
    confidence = check_website_liveness(website_url)
    company.contacts.setdefault("website", []).append(
        FieldValue(website_url, "проверка доступности сайта", date.today(), confidence)
    )


def _attach_site_contacts(company: Company, site_text: str, use_llm: bool = False) -> None:
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
    класса), должен иметь приоритет при отображении, если оба нашлись.

    Если `use_llm=True` и regex не нашёл ЧАСТЬ полей — один LLM-вызов
    (relevance_llm.extract_contacts) на ТЕ ЖЕ поля, что не нашлись, а не
    все три заново: то, что уже нашёл regex, — буквальное совпадение из
    текста, надёжнее генеративной догадки, LLM его не трогает и не
    переспрашивает (design-обсуждение: у regex нет "словаря" форматов,
    как у единиц измерения, контакты пишут как угодно — LLM здесь
    запасной вариант, а не замена)."""
    today = date.today()
    missing_fields: list[str] = []
    for field_name, pattern in (("phone", PHONE_RE), ("email", EMAIL_RE), ("address", ADDRESS_RE)):
        match = first_match(pattern, site_text)
        if match:
            company.contacts.setdefault(field_name, []).insert(
                0, FieldValue(match, "текст сайта (Слой 2)", today, VerificationFlag.UNVERIFIED)
            )
        else:
            missing_fields.append(field_name)

    if not use_llm or not missing_fields:
        return

    guess = extract_contacts(site_text)
    if guess is None:
        return
    llm_phone, llm_email, llm_address = guess
    for field_name, value in (("phone", llm_phone), ("email", llm_email), ("address", llm_address)):
        if field_name in missing_fields and value:
            company.contacts.setdefault(field_name, []).insert(
                0, FieldValue(value, "текст сайта (Слой 2, LLM)", today, VerificationFlag.UNVERIFIED)
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


# --- Заметки на будущее (перенесены из llm_classifier.py при его удалении) ---
# геогрифеская близовсть *
# сравнение из найденных характеристик с запрашиваемыми
# yandex_gen_search
# кол-во штук, бренд, мотоцикл кроссовый IRBIS 250 кубов - 4 шт (300 тоже можно, но меньше 200 - нет)
# лучше - но не хуже
# лок ллм -
# ген серч на рабочем ноуте
# находить конкретно по бренду товар
# Отзывы
# добавить какую-то систему рейтинга, более продуманную, сейчас обрал и оставил только ЕГРЮЛ на dadata
