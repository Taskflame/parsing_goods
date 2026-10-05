"""Оркестрация полного пайплайна (design_doc §3): [1]-[7] в одном вызове.

`search_and_score` и `run_pipeline` разделены, чтобы вызывающий код (CLI,
веб-API) мог получить список Company как данные — не только как файл на
диске. `run_pipeline` — тонкая обёртка для CLI-сценария "запрос -> Excel".
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from procurement_search.attribute_extractor import COUNT_UNITS, Quantity, classify_roles, extract_attributes
from procurement_search.availability import extract_availability as extract_product_availability
from procurement_search.brand_extractor import extract_brand
from procurement_search.config import (
    load_categories,
    load_marketplace_domains,
    load_scoring_weights,
    load_sources_config,
    load_spec_ranges,
    load_units,
)
from procurement_search.dedup import dedup_candidates
from procurement_search.enrichment import DadataEnricher, Enricher, NullEnricher
from procurement_search.export import export_companies_to_excel
from procurement_search.models import (
    ACTUAL_ADDRESS,
    Candidate,
    Company,
    FieldValue,
    ScoreBreakdown,
    StockStatus,
    VerificationFlag,
)
from procurement_search.query_normalizer import normalize_query
from procurement_search.query_kernel import condense_query
from procurement_search.quantity_match import (
    VERDICT_SORT_ORDER,
    Verdict,
    compare as compare_quantity,
    convert_to_unit,
)
from procurement_search.relevance_llm import (
    check_attribute_match,
    classify_category,
    classify_listing_type,
    classify_relevance,
    classify_stock_status,
    extract_contacts,
    extract_legal_name,
    extract_price,
)
from procurement_search.scoring import (
    combine_score,
    company_domain,
    compute_confidence,
    compute_score,
    compute_trust,
    has_attribute_mismatch,
    is_marketplace_domain,
    query_tokens,
    weights_for_category,
)
from procurement_search.site_relevance import compute_site_relevance, crawl_site_text
from procurement_search.stepper_probe import open_browser, probe_max_orderable_quantity
from procurement_search.sources.base import (
    ADDRESS_RE,
    EMAIL_RE,
    LEGAL_ENTITY_RE,
    PHONE_RE,
    PRICE_RE,
    first_match,
)
from procurement_search.sources.google_cse import build_default as build_google_cse
from procurement_search.sources.yandex_gen_search import build_default as build_yandex_gen_search
from procurement_search.sources.yandex_search import build_default as build_yandex_search
from procurement_search.trusted_suppliers import TrustedSupplierStore
from procurement_search.verify_contacts import check_website_liveness

logger = logging.getLogger(__name__)


def _effective_order_amount(order_qty: Quantity | None, order_length: Quantity | None) -> Quantity | None:
    """order_qty, если найден, иначе order_length, иначе None — единая
    цель для сравнения с остатком на сайте кандидата (Слой 4, экспорт,
    сводка). См. SearchResult.effective_order_amount, которая просто
    оборачивает этот же выбор для уже готового результата поиска."""
    return order_qty if order_qty is not None else order_length


class SearchResult(list):
    """list[Company], как и раньше — search_and_score всегда возвращала
    голый список, и десятки существующих вызовов (CLI, webapp.py, тесты)
    используют результат как список напрямую (`companies[0]`, `len(...)`,
    итерация). Подкласс list, а не namedtuple/dataclass-обёртка — тот же
    результат везде продолжает работать без изменений, но webapp.py/
    export.py дополнительно получают order_qty/order_length/product_description
    (см. attribute_extractor.ParsedQuery) для сводки по наличию
    (summarize_availability) без второго вызова classify_roles (и
    повторного LLM-вызова бренда/атрибутов при use_llm_fallback=True).

    order_qty (штуки/комплекты/...) и order_length (метраж — метры/см/мм/км)
    — независимые поля, каждое хранит СВОЁ значение как есть (см.
    attribute_extractor.ParsedQuery про то, почему они не одно и то же).
    Для мест, которым нужна ОДНА цель для сравнения с остатком на сайте
    (Слой 4, экспорт) — см. effective_order_amount()."""

    def __init__(
        self,
        companies,
        order_qty: Quantity | None = None,
        order_length: Quantity | None = None,
        product_description: str | None = None,
    ):
        super().__init__(companies)
        self.order_qty = order_qty
        self.order_length = order_length
        self.product_description = product_description

    def effective_order_amount(self) -> Quantity | None:
        """Что сравнивать с остатком на сайте кандидата (Слой 4, экспорт,
        сводка) — order_qty, если он найден, иначе order_length, иначе
        None. В подавляющем большинстве запросов заполнено что-то одно
        (запрос либо про штучный товар, либо про товар на отрез, см.
        classify_roles) — order_qty первый в приоритете просто потому, что
        он был первым в проекте, оба равноправны по смыслу."""
        return _effective_order_amount(self.order_qty, self.order_length)


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

# Слой 0 (доп.): поиск по базе доверенных поставщиков (trusted_suppliers.py) —
# сколько доменов одной категории пробовать за раз. Верхняя граница — не
# заваливать источники запросами, если доменов в базе для категории
# накопилось много (каждый домен — отдельный запрос на источник, см.
# _search_trusted_suppliers, а не один OR-запрос: синтаксис OR у Google
# CSE ("OR") и Yandex Search API ("|") разный, отдельный запрос на домен
# работает одинаково у обоих без специального разбора по источнику).
_MAX_TRUSTED_DOMAINS_TO_QUERY = 8

# Минимум уникальных компаний (после дедупа) от доверенных поставщиков,
# при котором глобальный поиск НЕ подключается вовсе — компромисс между
# "не тратить лишнюю квоту, когда категория уже прогрета" и "не обеднять
# выдачу, если доверенных пока мало". Эвристика, не измеренное значение —
# менять по мере накопления реальных данных.
_MIN_TRUSTED_CANDIDATES_TO_SKIP_GLOBAL_SEARCH = 3

# Сколько лучших (уже отсортированных) компаний финальной выдачи писать
# обратно в базу доверенных поставщиков за один поиск — top-N, а не
# top-1: тем же запросом уже подняты все данные, лишнего похода никуда
# не стоит, а глубина 3 позже позволит проверить, действительно ли топ-1
# был лучшим выбором, или байер регулярно выбирал второй/третий вариант —
# то, что топ-1 не даёт узнать в принципе (см. trusted_suppliers.py).
_TRUSTED_SUPPLIERS_WRITE_BACK_TOP_N = 3

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

# Ключи ответа relevance_llm.classify_stock_status -> models.StockStatus.
# Словарь, а не if/elif в _refine_relevance — тот же ключ используется как
# буквальное значение в json_schema-контракте LLM (llm_schemas.StockVerdict),
# менять их порознь означало бы держать соответствие в двух местах на глаз.
_STOCK_STATUS_MAP = {
    "in_stock": StockStatus.IN_STOCK,
    "clarify": StockStatus.CLARIFY,
    "out_of_stock": StockStatus.OUT_OF_STOCK,
}


def _parse_price_value(raw: str) -> float | None:
    """Число из строки цены (regex-матч PRICE_RE вида '15 000,50 ₽' или
    произвольный ответ LLM, см. extract_price) — только для сортировки в
    _ranking_key, отображается всё равно исходная строка (Company.price.value).

    Отбрасывает всё, кроме цифр/`.`/`,`, дальше решает, что запятая значит:
    десятичный разделитель (1-2 цифры после последней запятой — "1500,50")
    или разделитель тысяч, как пробел (иначе — "12,500" без копеек).
    Невалидное/нулевое значение -> None, а не 0.0 (0.0 в _ranking_key —
    легитимное место "самой дешёвой находки", 0 руб же ничего не значит и
    не должен обгонять реальные цены)."""
    cleaned = re.sub(r"[^\d.,]", "", raw)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        decimals = len(cleaned) - cleaned.rindex(",") - 1
        cleaned = cleaned.replace(",", ".") if decimals <= 2 else cleaned.replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def _ranking_key(
    company: Company, marketplace_domains: list[str]
) -> tuple[bool, int, bool, float, float]:
    """Пятиуровневая сортировка (по возрастанию):
      1. сначала не-маркетплейсы, потом маркетплейсы (см. is_marketplace_domain
         в scoring.py) — маркетплейсы не выкидываются из выдачи, а
         гарантированно оказываются НИЖЕ любого не-маркетплейса независимо
         от их score, всплывают в видимом топе только когда конкурентов не
         хватает;
      2. внутри каждой группы — сначала компании с НАЙДЕННОЙ ценой
         (Company.price, см. pipeline._attach_site_price), по возрастанию
         цены: жёсткая сортировка "дешевле — выше", которую байер явно
         запросил как ГЛАВНЫЙ KPI поверх уже готового списка (design-
         обсуждение — байер явно попросил именно цену первым уровнем, а
         не приоритет по наличию: у большинства сайтов количество вообще
         не публикуется, и если бы наличие стояло выше цены, дешёвая
         позиция с непубличным остатком тонула бы под дорогими
         кандидатами, у которых просто СЛУЧИЛOСЬ быть числом на сайте).
         Компании БЕЗ найденной цены в эту сортировку не участвуют и не
         штрафуются (тот же принцип "нет данных — не штраф", что и везде
         в scoring.py) — они просто идут отдельным блоком после всех
         отсортированных по цене;
      3. внутри одинаковой цены (или внутри блока "цена неизвестна") — по
         приоритету вердикта наличия (quantity_match.VERDICT_SORT_ORDER,
         см. search_and_score про check_availability): ENOUGH выше
         NOT_ENOUGH выше "нет данных" — поставщик с частичным остатком
         ("10 из 11") более действенная зацепка для байера, чем полное
         отсутствие данных об остатке, при прочих равных по цене.
         company.availability_verdict=None (флаг выключен, кандидат не
         входил в top-N, или сайт не отдал текст) получает тот же
         приоритет, что и Verdict.UNKNOWN;
      4. внутри одинаковой цены И одинакового вердикта — по убыванию
         score.total, как раньше.

    ИСТОРИЯ: было опробовано и ОТКАЧЕНО обратное — вердикт наличия выше
    цены (design-обсуждение: на живой выдаче это давало странный результат
    — дорогая позиция с найденным остатком обгоняла заметно более дешёвую
    без данных об остатке, хотя для большинства категорий товара остаток
    на сайте не публикуется почти никогда, то есть "выше цены" на практике
    означало "почти всегда выше цены" не по содержательной причине, а
    просто потому что у сортировки было мало реальных ничьих по цене).
    Байер явно попросил цену первым критерием — оставлено так.

    Явного исключения кандидатов БЕЗ известного количества из выдачи здесь
    нет и не будет (design-обсуждение: у подавляющего большинства
    поставщиков остаток на сайте вообще не публикуется — жёсткий фильтр
    оставлял бы выдачу почти пустой). "Нет в наличии" (OUT_OF_STOCK) — это
    отдельный, гораздо более сильный сигнал и убирается knockout'ом ДО
    сортировки (см. search_and_score), не через этот уровень.

    Цена в норме появляется только после Слоя 2 (_refine_relevance крадёт
    её из уже скачанного текста сайта, см. _attach_site_price) — то есть
    практически действует только при deep_relevance=True; без него у всех
    компаний price=None, и сортировка ведёт себя ровно как раньше.

    Применяется ДО среза `alive_companies[:deep_relevance_top_n]` в
    search_and_score — значит и бюджет краулинга Слоя 2/3 не тратится на
    маркетплейсы, пока есть чем его заполнить без них."""
    score = company.score.total if company.score else 0.0
    price_value = _parse_price_value(company.price.value) if company.price else None
    if company.availability_verdict is not None:
        verdict_priority = VERDICT_SORT_ORDER[Verdict(company.availability_verdict)]
    else:
        verdict_priority = VERDICT_SORT_ORDER[Verdict.UNKNOWN]
    return (
        is_marketplace_domain(company, marketplace_domains),
        price_value is None,
        price_value if price_value is not None else 0.0,
        verdict_priority,
        -score,
    )


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
    NullEnricher. Тот же паттерн, что у YANDEX_FM_API_KEY — фича
    включается наличием переменной окружения, без правки кода."""
    api_key = os.environ.get("DADATA_API_KEY")
    if api_key:
        return DadataEnricher(api_key=api_key)
    return NullEnricher()


def _search_trusted_suppliers(
    sources: list, trusted_domains: list[str], clean_query_text: str
) -> list[Candidate]:
    """Поиск, ограниченный уже известными доменами категории (см.
    search_and_score, use_trusted_suppliers) — один запрос НА ДОМЕН, а не
    один OR-запрос сразу на все домены: у Google CSE оператор OR — слово
    "OR", у Yandex Search API — "|" (см. sources/yandex_search.py),
    отдельный запрос на домен работает одинаково у обоих источников без
    специального разбора синтаксиса по каждому.

    `site:` — стандартный оператор ограничения по домену, поддержан и
    Google CSE, и Yandex Search API. clean_query_text (не raw_query) —
    то же "название товара без чисел/единиц измерения", что и у обычных
    search_terms (см. extract_attributes) — короче и без шума."""
    found: list[Candidate] = []
    for source in sources:
        for domain in trusted_domains:
            candidates = source.search(f"site:{domain} {clean_query_text}")
            logger.info(
                "%s: %d кандидатов по доверенному домену %r", source.name, len(candidates), domain
            )
            found.extend(candidates)
    return found


def search_and_score(
    raw_query: str,
    enricher: Enricher | None = None,
    use_llm_fallback: bool = False,
    verify_websites: bool = True,
    deep_relevance: bool = False,
    relevance_llm_check: bool = False,
    deep_relevance_top_n: int = 20,
    use_trusted_suppliers: bool = False,
    check_availability: bool = False,
    probe_stepper: bool = False,
) -> SearchResult:
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
    из `deep_relevance_top_n` кандидатов), поэтому опционально. Заодно
    единственный источник Company.price (см. _attach_site_price) — цена
    почти никогда не видна в сниппете поисковой выдачи, только на самой
    странице товара, поэтому жёсткая сортировка по цене в _ranking_key
    (см. её докстринг) практически действует только при deep_relevance=True.

    Слой 0.5 (scoring.has_attribute_mismatch) работает всегда, без флагов:
    числовые атрибуты запроса ("10000 л/час") извлекаются
    attribute_extractor.extract_attributes и сравниваются с тем же самым
    экстрактором на тексте кандидата (без LLM — дёшево, для всех
    кандидатов сразу). Явное расхождение в разы у совпадающей единицы
    измерения занижает relevance (не выкидывает компанию) — та же задача,
    что у relevance_llm.check_attribute_match (Слой 3), но бесплатная,
    грубее и работает на всей выдаче, а не только top-N.

    relevance_llm_check=False по умолчанию: требует переменных окружения
    Yandex AI Studio (YANDEX_FM_API_KEY/YANDEX_FM_MODEL). Включает два
    независимых LLM-фильтра:
      - _filter_non_listings (по сниппету, без краулинга) — исключает
        статьи/видео/обзоры, прошедшие Слой 1 по токенам, но не
        являющиеся страницей с товаром; работает всегда, deep_relevance
        не нужен;
      - Слой 3 (classify_relevance/check_attribute_match поверх Слоя 2,
        см. relevance_llm.py) — требует deep_relevance=True (нечего
        проверять без текста сайта), без него не действует.
      - classify_stock_status (тоже поверх Слоя 2, requires deep_relevance=True) —
        определяет статус наличия на сайте (в наличии / уточнить наличие /
        нет в наличии) и пишет его вместе с дословной цитатой-подтверждением
        в company.stock_status/stock_status_quote (models.StockStatus). В
        отличие от двух пунктов выше — НЕ влияет на score/ранжирование, это
        только информационная плашка для байера (см. export.py/webapp.py).

    use_trusted_suppliers=False по умолчанию: требует LLM (та же
    инфраструктура, что use_llm_fallback, но отдельный флаг — этот пишет
    в персистентную базу, use_llm_fallback ничего не сохраняет, смешивать
    их в одном флаге запутало бы, что именно включается). При включении:
      1. Слой 0 (доп.) — classify_category определяет категорию запроса
         (config/categories.yaml) одним LLM-вызовом;
      2. если категория нашлась и в trusted_suppliers.py есть под неё
         домены — сначала пробуем поиск, ограниченный ЭТИМИ доменами
         (_search_trusted_suppliers), и только если кандидатов после
         дедупа набралось меньше _MIN_TRUSTED_CANDIDATES_TO_SKIP_GLOBAL_SEARCH —
         подключаем сегодняшний неограниченный поиск (design-обсуждение:
         байер явно попросил именно такой порядок, а не "и то, и то
         сразу" — экономия квоты платных API на уже "прогретых"
         категориях);
      3. после скоринга top-N финальной выдачи (не top-1 — см.
         _TRUSTED_SUPPLIERS_WRITE_BACK_TOP_N) пишутся обратно в базу под
         резолвленной категорией — так база растёт с каждым поиском.

    check_availability=False по умолчанию: отдельный от relevance_llm_check
    флаг (та же инфраструктура Yandex AI Studio, но отдельная, более
    дорогая LLM-проверка — параллельно models.StockStatus/classify_stock_status,
    не заменяет её, см. models.AvailabilityStatus про мотивацию). Требует
    deep_relevance=True (нечего проверять без текста сайта top-N
    кандидатов, тот же текст, что уже скачан для Слоя 2/3 — без нового
    краулинга, см. availability.py); без deep_relevance включение флага —
    no-op с предупреждением в лог. Заполняет company.availability/
    availability_verdict/availability_verdict_text (quantity_match.compare)
    и переупорядочивает top-N по вердикту (VERDICT_SORT_ORDER) внутри
    равного score; кандидаты с вердиктом OUT_OF_STOCK исключаются из
    выдачи целиком (design-обсуждение: "нет в наличии" — это не просто
    низкий приоритет, показывать такого поставщика байеру бессмысленно,
    в отличие от NOT_ENOUGH/UNKNOWN, где звонок и довоз/уточнение всё ещё
    рабочий сценарий).

    probe_stepper=False по умолчанию: ПИЛОТ (см. stepper_probe.py) —
    отдельный от всего остального механизм, не LLM и не поиск-источник, а
    headless-браузер (Playwright), который реально открывает страницу и
    кликает по степперу количества ("−  1  +"), чтобы прочитать реакцию
    сайта (упёрлись в потолок остатка или нет) — сигнал, невидимый чистому
    HTTP-краулингу (JS-виджет). Требует check_availability=True (пробинг
    запускается только для кандидатов, у которых LLM-извлечение (Слой 4)
    НЕ дало уверенного вердикта — ENOUGH/OUT_OF_STOCK не трогаются, тратить
    браузер на них незачем) и пакета playwright (`pip install playwright &&
    playwright install chromium`, не входит в requirements.txt — тяжёлая
    опциональная зависимость только под этот флаг). Эвристика, не
    гарантия: единой вёрстки степпера не существует, часть сайтов вообще
    не проверяет остаток на фронте — probe_result=None (степпер не найден/
    страница не открылась) оставляет вердикт Слоя 4 как есть, не штрафует.
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

    # Бренд (brand_extractor.py) — открытый список, нет словарного пути,
    # только LLM, тот же флаг use_llm_fallback. Используется ниже как
    # приоритетный поисковый термин с брендом ПЕРВЫМ словом — в отличие
    # от clean_query_text, где бренд стоит там же, где его написал байер
    # (обычно не в начале). Не привязано к конкретному бренду/примеру —
    # работает для любого запроса, где LLM распознала имя производителя.
    brand = extract_brand(normalized.raw_query, use_llm_fallback=use_llm_fallback)

    # Слой 0 (доп.): сжатое ядро для длинных шаблонных запросов (query_kernel.py).
    # Тот же флаг use_llm_fallback, что и у бренда: длинный юридический текст
    # ("...Статья 23 ФЗ-458: ...") сжимается до сути ("обращение с отходами
    # III-IV классов опасности в Чувашии"), которая пойдёт в поисковик отдельным
    # термином. Для коротких запросов, выключенного флага или недоступного LLM
    # возвращает None — и поисковые термины строятся как раньше.
    kernel = condense_query(normalized.raw_query, use_llm_fallback=use_llm_fallback)

    # Слой 0.6 (см. attribute_extractor.classify_roles) — какие из чисел
    # query_extraction являются количеством ЗАКУПКИ (order_qty), а не
    # характеристикой товара. classify_roles — чистый пост-процессинг уже
    # готового query_extraction, не отдельный LLM-вызов (use_llm_fallback
    # уже мог быть потрачен внутри extract_attributes/extract_brand выше,
    # повторно тут не тратится).
    parsed_query = classify_roles(
        query_extraction, normalized.raw_query, brand, units_cfg, spec_ranges=load_spec_ranges()
    )
    if parsed_query.conflicts:
        for conflict in parsed_query.conflicts:
            logger.info("Слой 0.6: %s", conflict)
    # parsed_query.product — то же, что query_extraction.clean_text, за
    # исключением редкого маркерного order_qty ("нужно 5 компрессоров" —
    # "5" без единицы рядом не входит в matched_spans extract_attributes,
    # только classify_roles вырезает его отдельно). Для словарного order_qty
    # ("11 шт") оба значения уже совпадают — extract_attributes стирает
    # весь dict-матч из clean_text независимо от роли числа.
    clean_query_text = parsed_query.product

    # order_qty (например, "11 шт") — количество, которое нужно КУПИТЬ, не
    # характеристика товара, которую нужно НАЙТИ на сайте кандидата.
    # Дважды влияет на дальнейшее:
    #   1. query_attributes для has_attribute_mismatch (Слой 0.5) не должен
    #      содержать order_qty — иначе "11 шт" из запроса сравнивалось бы
    #      с любым "N шт" на сайте кандидата (артикул, фасовка, остаток на
    #      складе — что угодно с той же единицей) и ложно занижало бы
    #      relevance по случайному совпадению единицы измерения, не по
    #      смыслу характеристики товара.
    #   2. search_terms ниже не должен содержать order_qty — попадание
    #      количества закупки в поисковый запрос зашумляет выдачу Yandex/
    #      Google теми же цифрами, которые сами по себе не являются частью
    #      названия/характеристики искомого товара.
    #
    # order_length ("метраж закупки", см. attribute_extractor.LENGTH_UNITS) —
    # та же логика, тот же принцип, но исключается ТОЧЕЧНО, по конкретному
    # raw-тексту (parsed_query.order_length.raw), а не блокировкой всего
    # класса единиц длины — в отличие от COUNT_UNITS, единицы длины часто
    # остаются легитимной характеристикой ДРУГОГО экземпляра (см.
    # classify_roles: "труба 1.5 метра, 20 штук" — order_length там пуст,
    # а "1.5 метра" остаётся в query_attributes как обычная specs-величина).
    query_attributes = [a for a in query_extraction.attributes if a.unit not in COUNT_UNITS]
    if parsed_query.order_length is not None:
        query_attributes = [a for a in query_attributes if a.raw_text != parsed_query.order_length.raw]

    search_raw_query = normalized.raw_query
    for order_amount in (parsed_query.order_qty, parsed_query.order_length):
        if order_amount is None:
            continue
        idx = search_raw_query.find(order_amount.raw)
        if idx != -1:
            search_raw_query = re.sub(
                r"\s+",
                " ",
                search_raw_query[:idx] + search_raw_query[idx + len(order_amount.raw) :],
            ).strip()

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
    # Слой 0 (доп.): категория запроса — только ключ для базы доверенных
    # поставщиков (design-обсуждение отличает это от прежнего, выпиленного
    # categories.yaml, см. docs/design_doc.md §4: здесь у категории есть
    # реальный потребитель, а не dead code). category_code остаётся None,
    # если флаг выключен, LLM недоступна, или сама модель не нашла
    # подходящей категории — во всех случаях просто работаем как раньше.
    category_code: str | None = None
    used_trusted_suppliers = False
    if use_trusted_suppliers:
        categories_cfg = load_categories()
        category_code = classify_category(normalized.raw_query, categories_cfg)
        if category_code is None:
            logger.info(
                "Запрос %r не отнесён ни к одной категории (LLM недоступна или "
                "не нашла подходящей) — доверенные поставщики не подключаются",
                normalized.raw_query,
            )
        else:
            category_name = categories_cfg.get(category_code, {}).get("name", category_code)
            logger.info("Запрос %r отнесён к категории %s (%r)", normalized.raw_query, category_code, category_name)
            with TrustedSupplierStore() as store:
                trusted_domains = store.domains_for_category(category_code)[:_MAX_TRUSTED_DOMAINS_TO_QUERY]
            if not trusted_domains:
                logger.info(
                    "Категория %r: доверенных доменов в базе ещё нет — сразу "
                    "обычный глобальный поиск (база начнёт заполняться по итогам этого запроса)",
                    category_code,
                )
            else:
                trusted_candidates = _search_trusted_suppliers(sources, trusted_domains, clean_query_text)
                # Кандидаты добавляются в общий пул независимо от исхода
                # ниже — уже сделанная работа не выбрасывается, даже если
                # доверенных оказалось недостаточно и подключается
                # глобальный поиск (он ДОПОЛНЯЕТ эти кандидаты, а не
                # заменяет их).
                candidates.extend(trusted_candidates)
                trusted_groups = dedup_candidates(trusted_candidates)
                if len(trusted_groups) >= _MIN_TRUSTED_CANDIDATES_TO_SKIP_GLOBAL_SEARCH:
                    logger.info(
                        "Категория %r: %d доверенных кандидатов после дедупа — "
                        "глобальный поиск пропущен",
                        category_code,
                        len(trusted_groups),
                    )
                    used_trusted_suppliers = True
                else:
                    logger.info(
                        "Категория %r: доверенных кандидатов недостаточно (%d < %d) — "
                        "подключаем обычный глобальный поиск",
                        category_code,
                        len(trusted_groups),
                        _MIN_TRUSTED_CANDIDATES_TO_SKIP_GLOBAL_SEARCH,
                    )

    if not used_trusted_suppliers:
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
        # search_raw_query, а не normalized.raw_query — с вырезанным order_qty
        # (см. выше про то, почему количество закупки не должно уходить в
        # поисковый запрос); при order_qty=None это просто raw_query как раньше.
        extra_terms = [clean_query_text]
        if brand:
            without_brand = re.sub(re.escape(brand), "", clean_query_text, flags=re.IGNORECASE)
            without_brand = re.sub(r"\s+", " ", without_brand).strip()
            brand_first_term = f"{brand} {without_brand}".strip() if without_brand else brand
            extra_terms.insert(0, brand_first_term)
        # Слой 0 (query_kernel.py, см. выше kernel): сжатое ядро длинного
        # шаблонного запроса — короткая осмысленная формулировка, понятная
        # поисковику без юридической каши. Добавляется как отдельный термин.
        # Длинный raw_query при этом НЕ выбрасывается: раз есть ядро, разрешаем
        # до 4 поисковых запросов к источнику вместо 3, чтобы ушли и ядро, и
        # полный оригинал (ничего не теряется, дедуп всё равно схлопнется).
        if kernel:
            extra_terms.insert(0, kernel)
        max_terms = 4 if kernel else 3
        search_terms = list(dict.fromkeys([search_raw_query, *extra_terms]))
        for source in sources:
            # raw_query + (ядро) + бренд-термин + clean_text, не больше max_terms
            for term in search_terms[:max_terms]:
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
            enricher=enricher,
            check_availability=check_availability,
            product_description=parsed_query.product,
            order_qty=_effective_order_amount(parsed_query.order_qty, parsed_query.order_length),
            units=units_cfg,
            probe_stepper=probe_stepper,
        )
        # Слой 2 мог дорезолвить компанию в ЕГРЮЛ по названию юрлица со
        # своего же сайта (см. _attach_legal_name) и обнаружить, что она
        # на самом деле ликвидирована — а не просто "неизвестно", как
        # выглядело при первой попытке по заголовку товарной карточки
        # (enrichment.py). Ту же knockout-отсечку, что и раньше (ТЗ п.4),
        # нужно применить и здесь, иначе такая компания проскочит в выдачу
        # только потому, что попала в неё ДО того, как реально резолвилась.
        before_knockout = len(alive_companies)
        alive_companies = [c for c in alive_companies if not _is_knockout(c)]
        if len(alive_companies) != before_knockout:
            logger.info(
                "Исключено %d компаний после Слоя 2 — дорезолвились в ЕГРЮЛ как "
                "ликвидированные (первая попытка резолвинга по заголовку карточки "
                "товара их не находила, см. _attach_legal_name)",
                before_knockout - len(alive_companies),
            )

        if check_availability:
            # "Нет в наличии" — не просто низкий приоритет (в отличие от
            # NOT_ENOUGH/UNKNOWN, где звонок и довоз/уточнение всё ещё
            # рабочий сценарий, см. quantity_match.VERDICT_SORT_ORDER):
            # показывать байеру заведомо нерабочего поставщика бессмысленно,
            # тот же knockout-паттерн, что и у ЕГРЮЛ-отсечки выше, с тем же
            # обязательным логированием (design-обсуждение весов: "это не
            # баллы, это бинарное «выкинуть»").
            before_oos = len(alive_companies)
            alive_companies = [
                c for c in alive_companies if c.availability_verdict != Verdict.OUT_OF_STOCK.value
            ]
            if len(alive_companies) != before_oos:
                logger.info(
                    "Исключено %d компаний — Слой 4 (check_availability) подтвердил "
                    "отсутствие товара на сайте",
                    before_oos - len(alive_companies),
                )

        alive_companies.sort(key=lambda c: _ranking_key(c, marketplace_domains))
    elif check_availability or probe_stepper:
        logger.warning(
            "check_availability=True/probe_stepper=True требуют deep_relevance=True (нечего "
            "проверять без текста сайта top-N кандидатов, см. availability.py) — проверка "
            "наличия/пробинг степпера пропущены"
        )

    if use_trusted_suppliers and category_code is not None:
        # search_and_score к этому моменту уже потратил десятки секунд на
        # краулинг/LLM (см. README §Деплой — несколько одновременных
        # пользователей на сервере) — запись в SQLite здесь дополнительная
        # польза на будущее, а не то, ради чего человек ждал результат.
        # database is locked (при настоящей конкурентной записи с другого
        # одновременного поиска/добавления через UI, см.
        # TrustedSupplierStore.__init__ про timeout=30/WAL) не должна
        # выбрасывать уже посчитанный результат целиком.
        try:
            with TrustedSupplierStore() as store:
                _write_back_trusted_suppliers(
                    store,
                    alive_companies,
                    category_code,
                    normalized.raw_query,
                    marketplace_domains,
                    _TRUSTED_SUPPLIERS_WRITE_BACK_TOP_N,
                )
        except sqlite3.OperationalError:
            logger.warning(
                "Не удалось записать write-back в trusted_suppliers.db (база занята "
                "конкурентной записью) — результат поиска не пострадал, просто эти "
                "находки не попадут в базу доверенных поставщиков",
                exc_info=True,
            )

    return SearchResult(
        alive_companies,
        order_qty=parsed_query.order_qty,
        order_length=parsed_query.order_length,
        product_description=parsed_query.product,
    )


def _refine_relevance(
    companies: list[Company],
    raw_query: str,
    tokens: set[str],
    weights: dict,
    use_llm: bool,
    enricher: Enricher,
    check_availability: bool = False,
    product_description: str | None = None,
    order_qty: Quantity | None = None,  # уже "эффективная" цель — см. _effective_order_amount:
    # штучное количество ИЛИ метраж, что нашлось в запросе (см. pipeline._effective_order_amount)
    units: dict | None = None,
    probe_stepper: bool = False,
) -> None:
    """Слои 2-4 уточнения релевантности (design-обсуждение скоринга) — на
    входе уже отранжированный по грубому Слою-1-скору срез top-N, не вся
    выдача (краулинг+LLM на 100-200 кандидатов не оправданы по времени/
    стоимости). Мутирует company.score (и, при check_availability=True,
    company.availability/availability_verdict/availability_verdict_text)
    на месте.

    Если у кандидата нет сайта или он не отдался — relevance не трогается,
    остаётся значением Слоя 1 (нет данных для уточнения — не выдумываем).
    Тот же текст сайта, что и для Слоя 2/3 — availability.extract_availability
    не делает отдельного HTTP-запроса (см. её докстринг).

    probe_stepper=True (пилот, см. stepper_probe.py) — дополнительно, ПОСЛЕ
    LLM-извлечения (check_availability обязателен), открывает headless-
    браузер и пытается довести степпер количества на странице до order_qty
    кликами по "+" — только для кандидатов с НЕЯСНЫМ вердиктом
    (_STEPPER_PROBE_ELIGIBLE_VERDICTS), не тратится на уже уверенные ENOUGH/
    OUT_OF_STOCK/ON_ORDER/UNIT_MISMATCH. Один браузер на весь вызов (не по
    экземпляру на кандидата — запуск Chromium сам по себе стоит секунды,
    так дороже на порядок), закрывается в конце независимо от того, был ли
    хоть один успешный пробинг."""
    browser = None
    playwright_ctx = None
    if probe_stepper and not check_availability:
        logger.warning(
            "probe_stepper=True требует check_availability=True (пробинг уточняет только "
            "неясные вердикты Слоя 4, без него не от чего отталкиваться) — пробинг пропущен"
        )
    elif probe_stepper:
        try:
            playwright_ctx, browser = open_browser()
        except ImportError:
            logger.warning(
                "probe_stepper=True, но пакет playwright не установлен "
                "(pip install playwright && playwright install chromium) — пробинг степпера пропущен"
            )

    try:
        _refine_relevance_loop(
            companies,
            raw_query=raw_query,
            tokens=tokens,
            weights=weights,
            use_llm=use_llm,
            enricher=enricher,
            check_availability=check_availability,
            product_description=product_description,
            order_qty=order_qty,
            units=units,
            browser=browser,
        )
    finally:
        if browser is not None:
            browser.close()
        if playwright_ctx is not None:
            playwright_ctx.stop()


# Вердикты, при которых пробинг степпера (probe_stepper) имеет смысл —
# ENOUGH/OUT_OF_STOCK уже уверенные ответы LLM, ON_ORDER/UNIT_MISMATCH —
# отдельные состояния, которые клик по "+" не проясняет (срок поставки/
# несопоставимые единицы, не вопрос "сколько можно добавить в корзину").
_STEPPER_PROBE_ELIGIBLE_VERDICTS = {
    Verdict.UNKNOWN.value,
    Verdict.NOT_ENOUGH.value,
    Verdict.IN_STOCK_NO_QTY.value,
}


def _refine_relevance_loop(
    companies: list[Company],
    raw_query: str,
    tokens: set[str],
    weights: dict,
    use_llm: bool,
    enricher: Enricher,
    check_availability: bool,
    product_description: str | None,
    order_qty: Quantity | None,  # эффективная цель — см. _effective_order_amount
    units: dict | None,
    browser,
) -> None:
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
        _attach_site_price(company, site_text, use_llm=use_llm)
        _attach_legal_name(company, site_text, enricher, use_llm=use_llm)

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
            # поставщика по нему было бы неоправданно). Три категории, не
            # бинарно: сайты пишут не только "есть"/"нет", но и "уточняйте
            # у менеджера"/"цена по запросу" — раньше это тихо схлопывалось
            # в "в наличии" по умолчанию, теперь отдельная категория
            # CLARIFY. stock_status_quote — дословная фраза с сайта рядом с
            # категорией, чтобы байер видел не только вывод модели, а и то,
            # на основании чего он сделан.
            stock_result = classify_stock_status(site_text)
            if stock_result is not None:
                status, quote = stock_result
                company.stock_status = _STOCK_STATUS_MAP[status]
                company.stock_status_quote = quote
            # None (LLM недоступна/упала) — stock_status остаётся
            # NOT_CHECKED, не выдумываем результат.

        if check_availability:
            # Слой 4 (models.Availability, quantity_match.py) — отдельный
            # от use_llm флаг (не завязан на relevance_llm_check, см.
            # search_and_score про мотивацию раздельной стоимости).
            # product_description — parsed_query.product (Слой 0.6), а не
            # raw_query целиком: LLM не должна путать искомый товар с
            # количеством закупки, которого на странице кандидата и не
            # может быть.
            availability = extract_product_availability(
                product_description or raw_query, site_text, website_entries[0].value, units
            )
            company.availability = availability
            verdict, verdict_text = compare_quantity(order_qty, availability)
            company.availability_verdict = verdict.value
            company.availability_verdict_text = verdict_text

            # Резерв на случай, если _attach_site_price выше (отдельный
            # LLM-вызов extract_price + regex-фоллбек) не нашёл цену, а
            # этот, другой по промпту LLM-вызов (extract_availability) её
            # всё-таки заметил — оба смотрят на один и тот же site_text,
            # но с разными задачами, и на практике не всегда совпадают
            # (design-обсуждение: реальный кейс, где extract_price
            # промахнулся, а extract_availability попутно вернул price
            # в том же ответе). Не наоборот — _attach_site_price
            # специализирована именно на цене и должна иметь приоритет,
            # когда у неё результат есть.
            if company.price is None and availability.price:
                parsed_price = _parse_price_value(availability.price)
                if parsed_price is not None:
                    company.price = FieldValue(
                        availability.price,
                        "текст сайта (Слой 4, LLM)",
                        date.today(),
                        VerificationFlag.UNVERIFIED,
                    )

            # Пилот (см. stepper_probe.py): LLM-извлечение выше не дало
            # уверенного ответа — пробуем интерактивно, кликами по "+" на
            # самой странице, довести степрер до order_qty. Только для
            # неясных вердиктов (_STEPPER_PROBE_ELIGIBLE_VERDICTS) — нет
            # смысла тратить секунды на браузер там, где LLM уже дала
            # уверенный ENOUGH/OUT_OF_STOCK/ON_ORDER/UNIT_MISMATCH.
            if (
                browser is not None
                and order_qty is not None
                and verdict.value in _STEPPER_PROBE_ELIGIBLE_VERDICTS
            ):
                probe_result = probe_max_orderable_quantity(
                    website_entries[0].value,
                    order_qty.value,
                    browser=browser,
                    product_description=product_description,
                )
                if probe_result is not None:
                    if probe_result.target_confirmed:
                        company.availability_verdict = Verdict.ENOUGH.value
                        company.availability_verdict_text = (
                            f"Подтверждено интерактивной проверкой (степпер количества на "
                            f"сайте): доступно от {order_qty.value:g} {order_qty.unit}"
                        )
                    else:
                        company.availability_verdict = Verdict.NOT_ENOUGH.value
                        evidence_part = f" ({probe_result.evidence})" if probe_result.evidence else ""
                        company.availability_verdict_text = (
                            f"Степпер на сайте не дал добавить больше "
                            f"{probe_result.max_orderable:g} из {order_qty.value:g} "
                            f"{order_qty.unit}{evidence_part}"
                        )
                    # Не наоборот: probe_result=None (степпер не найден/страница
                    # не открылась) оставляет вердикт Слоя 4 как есть — "нет
                    # данных от пробинга" не значит "нет данных вообще".

        # trust/confidence пересчитываются заново, не берутся из старого
        # company.score — _attach_legal_name выше мог только что дорезолвить
        # компанию в ЕГРЮЛ (inn/status/адрес поменялись), и trust/confidence
        # Слоя 1, посчитанные ДО этого по "неизвестно"/inn=None, устарели
        # бы и разошлись с company.status/company.inn на экране (design-
        # обсуждение: цена этого пересчёта пренебрежимо мала — это чистая
        # функция от уже готовых полей Company, без сети).
        trust = compute_trust(company, weights)
        confidence = compute_confidence(company)
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
                0,
                FieldValue(
                    match,
                    "текст сайта (Слой 2)",
                    today,
                    VerificationFlag.UNVERIFIED,
                    kind=(ACTUAL_ADDRESS if field_name == "address" else None),
                ),
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
                0,
                FieldValue(
                    value,
                    "текст сайта (Слой 2, LLM)",
                    today,
                    VerificationFlag.UNVERIFIED,
                    kind=(ACTUAL_ADDRESS if field_name == "address" else None),
                ),
            )


def _attach_site_price(company: Company, site_text: str, use_llm: bool = False) -> None:
    """Слой 2 (доп.): цена товара с сайта кандидата — переиспользует уже
    скачанный текст сайта (та же логика, что у _attach_site_contacts, но
    цена не contacts-словарь с несколькими источниками, а единственное
    поле Company.price, см. models.py: второго источника цены, кроме
    самого сайта, у нас нет).

    Порядок ОБРАТНЫЙ по сравнению с _attach_site_contacts (там сначала
    regex — точное совпадение надёжнее догадки): при `use_llm=True`
    сначала спрашиваем LLM (relevance_llm.extract_price), и только если
    она ничего не вернула (недоступна/упала, или на странице правда нет
    цены) — пробуем PRICE_RE как последний шанс. Причина именно для цены:
    даже на верной странице товара (см. site_relevance.crawl_site_text
    про баг с потерянным путём ссылки) рядом часто соседствуют НЕСКОЛЬКО
    чисел с валютой — старая зачёркнутая цена по акции, блок "похожие
    товары"/"с этим покупают", стоимость доставки — а PRICE_RE не понимает
    контекст и берёт первое совпадение по странице, не обязательно то, что
    относится к запрошенному товару (design-обсуждение: реально
    наблюдалось на живой выдаче — вместо 48 219 ₽ регекс находил "100 ₽"
    из совсем другого места страницы). LLM видит текст целиком и может
    отличить нужную цену от соседних чисел.

    UNVERIFIED, как и остальные поля со скрапинга — цена меняется быстрее,
    чем следующий перезапуск поиска, независимая проверка не предусмотрена.

    Оба пути (LLM и regex) дополнительно проверяются через
    _parse_price_value: "0 руб."/"0 640 ₽" реально встречаются на живой
    выдаче (design-обсуждение) — не опечатка модели и не брак regex, а
    похоже на плейсхолдер JS-виджета динамической цены ("0" до того, как
    скрипт на странице подставит настоящее значение), который остаётся в
    HTML как есть, потому что наш краулер не исполняет JavaScript (см.
    site_relevance.crawl_site_text — тот же класс ограничения, что и у
    DuckDuckGo-антибота). Ноль/нечитаемое значение — не более достоверная
    цена, чем её отсутствие, поэтому такой матч отбрасывается целиком,
    а не сохраняется как есть."""
    match = None
    source = None
    if use_llm:
        candidate = extract_price(site_text)
        if candidate and _parse_price_value(candidate) is not None:
            match, source = candidate, "текст сайта (Слой 2, LLM)"
    if not match:
        candidate = first_match(PRICE_RE, site_text)
        if candidate and _parse_price_value(candidate) is not None:
            match, source = candidate, "текст сайта (Слой 2)"
    if not match:
        return
    company.price = FieldValue(match, source, date.today(), VerificationFlag.UNVERIFIED)


def _attach_legal_name(
    company: Company, site_text: str, enricher: Enricher, use_llm: bool = False
) -> None:
    """Слой 2 (доп.): повторная попытка резолвинга компании в ЕГРЮЛ — см.
    Enricher.re_resolve (enrichment.py). Первая попытка (enricher.build_company,
    вызывается ДО краулинга) резолвит по Candidate.name_raw — для активных
    источников (google_cse.py/yandex_search.py) это заголовок ТОВАРНОЙ
    карточки из поисковой выдачи ("Генератор бензиновый Huter DY3000L"), а
    не название организации, и Dadata suggest/party по такому запросу
    почти никогда не находит матч (design-обсуждение, реальный кейс с
    живой выдачи: у всех компаний в отчёте status="неизвестно"). Здесь
    ищем настоящее название юрлица на самом сайте кандидата (обычно в
    футере/разделе "Реквизиты" — LEGAL_ENTITY_RE в sources/base.py, с
    LLM-фоллбеком при use_llm=True, как у _attach_site_price) и просим
    enricher попробовать снова, уже с ним.

    Ничего не делает, если компания уже резолвлена (company.inn задан —
    нет смысла тратить regex/LLM на название, которое всё равно никуда не
    пойдёт) или enricher — NullEnricher (нечему резолвить). Проверка
    company.inn здесь, а не только внутри enricher.re_resolve — чтобы не
    тратить LLM-вызов впустую ещё ДО похода к Dadata, а не после.

    Известное ограничение: если название юрлица со Слоя 2 всё-таки не
    совпадает с реальным поставщиком (омонимы, дочерние компании,
    франшиза) — _pick_best_suggestion (enrichment.py) подбирает вариант по
    пересечению адреса, та же эвристика и те же её границы, что и у
    первой попытки резолвинга."""
    if company.inn is not None or isinstance(enricher, NullEnricher):
        return
    legal_name = first_match(LEGAL_ENTITY_RE, site_text)
    if not legal_name and use_llm:
        legal_name = extract_legal_name(site_text)
    if not legal_name:
        return
    enricher.re_resolve(company, legal_name, company.raw_candidates)


def _first_contact_value(company: Company, field_name: str) -> str | None:
    values = company.contacts.get(field_name, [])
    return values[0].value if values else None


def _write_back_trusted_suppliers(
    store: TrustedSupplierStore,
    companies: list[Company],
    category_code: str,
    raw_query: str,
    marketplace_domains: list[str],
    top_n: int,
) -> None:
    """Пишет top_n лучших (уже отсортированных) компаний финальной выдачи
    в базу доверенных поставщиков под резолвленной категорией — top_n, а
    не top-1: см. _TRUSTED_SUPPLIERS_WRITE_BACK_TOP_N про мотивацию
    (топ-1 не позволяет потом проверить, действительно ли байер выбирал
    именно первый вариант).

    Маркетплейсы (is_marketplace_domain) не попадают в базу — это не
    поставщик, а розничная площадка (см. её докстринг в scoring.py).
    Компании без резолвленного домена сайта (scoring.company_domain
    вернула None — ни у одного кандидата в группе не было собственного
    сайта) тоже пропускаются: домен — обязательный ключ таблицы
    suppliers в trusted_suppliers.py, писать без него нечего. Такие
    компании просто не считаются в top_n — следующая по списку компания
    занимает освободившееся место, а не пропуск целиком обрывает запись."""
    written = 0
    for company in companies:
        if written >= top_n:
            break
        if is_marketplace_domain(company, marketplace_domains):
            continue
        domain = company_domain(company)
        if domain is None:
            continue
        store.record_supplier(
            domain=domain,
            name=company.name.value,
            category_code=category_code,
            rank=written + 1,
            source_query=raw_query,
            inn=company.inn,
            phone=_first_contact_value(company, "phone"),
            email=_first_contact_value(company, "email"),
            address=_first_contact_value(company, "address"),
        )
        written += 1


def summarize_availability(companies: list[Company], order_qty: Quantity | None) -> str | None:
    """Одна строка сводки над таблицей (export.py/webapp.py) — сколько
    требуется (в единице order_qty — штуки, комплекты, метры, что угодно,
    см. pipeline._effective_order_amount), сколько реально подтверждено на
    складах top-N кандидатов, и закрывает ли вопрос один поставщик. None —
    order_qty не распознан (нечего сравнивать) или ни у одного кандидата
    нет company.availability.quantity в сопоставимой единице
    (check_availability выключен, LLM не нашла числа, или единицы не
    совпадают ни напрямую, ни через фасовку) — тот же принцип "нет данных
    — молчим, а не гадаем", что и везде в scoring.py/quantity_match.py."""
    if order_qty is None:
        return None

    confirmed_total = 0.0
    contributors = 0
    best_company: Company | None = None
    best_amount = 0.0
    for company in companies:
        availability = company.availability
        if availability is None or availability.quantity is None:
            continue
        converted = convert_to_unit(availability.quantity, availability.pack_size, order_qty.unit)
        if converted is None:
            continue
        confirmed_total += converted
        contributors += 1
        if converted > best_amount:
            best_amount = converted
            best_company = company

    if contributors == 0:
        return None

    lines = [
        f"Требуется: {order_qty.value:g} {order_qty.unit}",
        f"Подтверждено на складах: {confirmed_total:g} {order_qty.unit} у {contributors} поставщиков",
    ]
    if best_company is not None and best_amount >= order_qty.value:
        lines.append(
            f"Одним поставщиком закрывается: да ({best_company.name.value} — {best_amount:g} {order_qty.unit})"
        )
    else:
        lines.append("Одним поставщиком закрывается: нет")
    return "\n".join(lines)


def run_pipeline(
    raw_query: str,
    output_path: str | Path,
    enricher: Enricher | None = None,
    use_llm_fallback: bool = False,
    verify_websites: bool = True,
    deep_relevance: bool = False,
    relevance_llm_check: bool = False,
    use_trusted_suppliers: bool = False,
    check_availability: bool = False,
    probe_stepper: bool = False,
) -> Path:
    """Прогоняет запрос через весь пайплайн и пишет результат в Excel (CLI-сценарий)."""
    result = search_and_score(
        raw_query,
        enricher=enricher,
        use_llm_fallback=use_llm_fallback,
        verify_websites=verify_websites,
        deep_relevance=deep_relevance,
        relevance_llm_check=relevance_llm_check,
        use_trusted_suppliers=use_trusted_suppliers,
        check_availability=check_availability,
        probe_stepper=probe_stepper,
    )
    effective_amount = result.effective_order_amount()
    summary = summarize_availability(result, effective_amount)
    return export_companies_to_excel(result, output_path, required_qty=effective_amount, summary=summary)


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
