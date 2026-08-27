"""Слой 3 уточнения релевантности — точечная LLM-проверка (design-
обсуждение скоринга, см. scoring.py про Слой 1 и site_relevance.py про
Слой 2). Вопрос вида "есть ли на этом сайте <товар> как реальная позиция
номенклатуры, а не случайное упоминание" — Слой 2 (пересечение токенов по
тексту сайта) этого не отличает, для этого и нужна LLM.

Применяется ТОЛЬКО к top-N кандидатам, уже прошедшим Слои 1-2 (см.
pipeline.py) — вызывать LLM на каждого из 100-200 кандидатов было бы
медленно и (для платного провайдера) дорого.

Провайдер и паттерн выбора — те же, что в query_normalizer._try_llm_fallback
(ленивый импорт yandexgpt_classifier.py, любая ошибка -> None, а не падение
пайплайна) — переиспользуем инфраструктуру, а не заводим новую.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def classify_relevance(raw_query: str, site_text: str) -> bool | None:
    """None — LLM недоступна (сеть/ключ/пакет не установлен) или упала —
    вызывающий код должен трактовать это как "нет сигнала", а не как
    "нерелевантно" (см. pipeline.py: relevance не трогается, если None)."""
    try:
        from procurement_search.yandexgpt_classifier import (
            classify_relevance_with_yandexgpt as classify_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — LLM-проверка релевантности (Слой 3) пропущена")
        return None

    try:
        result = classify_fn(raw_query, site_text)
    except Exception:
        logger.warning(
            "LLM-проверка релевантности для запроса %r не сработала", raw_query, exc_info=True
        )
        return None
    return result.is_relevant


def check_attribute_match(raw_query: str, site_text: str) -> bool | None:
    """Вторая LLM-проверка Слоя 3, отдельная от classify_relevance: та
    отвечает "товар вообще продаётся на этом сайте", эта — "совпадают ли
    его конкретные характеристики (числовые и качественные) с тем, что
    запросил байер" (design-обсуждение: "дренажный насос 10000 л/час" не
    должен матчиться с найденным насосом на 18 л/ч).

    None — LLM недоступна/упала, как и у classify_relevance: трактуется
    вызывающим кодом как "нет сигнала", соответствие не трогается."""
    try:
        from procurement_search.yandexgpt_classifier import (
            classify_attribute_match_with_yandexgpt as classify_fn,
        )
    except ImportError:
        logger.warning(
            "Пакет openai не установлен — LLM-проверка соответствия характеристик (Слой 3) пропущена"
        )
        return None

    try:
        result = classify_fn(raw_query, site_text)
    except Exception:
        logger.warning(
            "LLM-проверка соответствия характеристик для запроса %r не сработала",
            raw_query,
            exc_info=True,
        )
        return None
    return result.matches


def classify_stock_status(site_text: str) -> bool | None:
    """Слой 3, информационная проверка (см. models.StockStatus): True — на
    сайте нашлась явная плашка "нет в наличии"/"товар закончился"/"распродано",
    False — такого маркера не нашлось (не путать с "точно в наличии" — просто
    нет сигнала об обратном). None — LLM недоступна/упала, вызывающий код
    (pipeline._refine_relevance) должен трактовать это как "не проверено" —
    как и остальной Слой 3, НЕ влияет на score, только на company.stock_status."""
    try:
        from procurement_search.yandexgpt_classifier import (
            classify_stock_status_with_yandexgpt as classify_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — LLM-проверка наличия товара (Слой 3) пропущена")
        return None

    try:
        result = classify_fn(site_text)
    except Exception:
        logger.warning("LLM-проверка наличия товара не сработала", exc_info=True)
        return None
    return result.out_of_stock


def classify_listing_type(raw_query: str, title: str, snippet: str | None) -> bool | None:
    """Дешёвый предфильтр по заголовку+сниппету (не требует краулинга —
    см. yandexgpt_classifier.classify_listing_type_with_yandexgpt про
    мотивацию: статьи/видео/обзоры проходят Слой 1 по токенному
    пересечению, но не являются страницей, где товар можно купить). В
    отличие от classify_relevance/check_attribute_match, вызывающий код
    (pipeline.py) трактует False здесь как ЖЁСТКОЕ исключение (design-
    обсуждение: у статьи/видео нет ни оффера, ни контактов, доставать
    нечего — в отличие от маркетплейса, который теоретически мог бы быть
    местом покупки).

    None — LLM недоступна/упала: трактуется как "нет сигнала", компания не
    исключается (не хотим ронять байера в 0 кандидатов из-за сетевой
    ошибки у LLM-провайдера)."""
    try:
        from procurement_search.yandexgpt_classifier import (
            classify_listing_type_with_yandexgpt as classify_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — LLM-фильтр типа контента пропущен")
        return None

    try:
        result = classify_fn(raw_query, title, snippet)
    except Exception:
        logger.warning(
            "LLM-фильтр типа контента для запроса %r не сработал", raw_query, exc_info=True
        )
        return None
    return result.is_listing


def extract_price(site_text: str) -> str | None:
    """Запасной вариант к regex-извлечению цены (sources/base.py PRICE_RE) —
    см. pipeline._attach_site_price, вызывается только если regex не нашёл
    цену. Возвращает цену дословно как написано (с валютой), либо None —
    LLM недоступна/упала, или цены на странице нет вообще; вызывающий код
    трактует оба случая одинаково: "цена неизвестна", компания не
    штрафуется (см. pipeline._ranking_key)."""
    try:
        from procurement_search.yandexgpt_classifier import (
            extract_price_with_yandexgpt as extract_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — LLM-извлечение цены (Слой 2) пропущено")
        return None

    try:
        result = extract_fn(site_text)
    except Exception:
        logger.warning("LLM-извлечение цены не сработало", exc_info=True)
        return None
    return result.price


def classify_category(raw_query: str, categories: dict[str, dict]) -> str | None:
    """Слой 0 (доп.): категория закупок для запроса — см.
    pipeline._attach_trusted_suppliers, ключ для поиска доверенных
    поставщиков (trusted_suppliers.py). None — LLM недоступна/упала, или
    сама модель не нашла подходящей категории (CategoryGuess.category is
    None) — в обоих случаях вызывающий код просто не подключает
    доверенных поставщиков и не пишет в базу, работает как раньше
    (полагаясь на глобальный поиск)."""
    try:
        from procurement_search.yandexgpt_classifier import (
            classify_category_with_yandexgpt as classify_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — LLM-классификация категории пропущена")
        return None

    try:
        result = classify_fn(raw_query, categories)
    except Exception:
        logger.warning("LLM-классификация категории для запроса %r не сработала", raw_query, exc_info=True)
        return None
    return _normalize_category_code(result.category, categories)


def _normalize_category_code(raw_category: str | None, categories: dict[str, dict]) -> str | None:
    """Живой кейс: несмотря на явную инструкцию "верни только код",
    модель иногда возвращает "F3: СИЛОВОЕ ЭЛЕКТРООБОРУДОВАНИЕ..." вместо
    просто "F3" — тот же принцип, что и у валидации цены/ноля в
    pipeline._attach_site_price: формату ответа LLM нельзя доверять
    вслепую, дальше по коду category_code используется как ключ словаря
    (categories_cfg[category_code]) и как ключ в trusted_suppliers.py —
    некорректный код там не упал бы сразу, а тихо не находил бы ничего
    (get(...) с дефолтом/пустой список доменов), что хуже честного None."""
    if raw_category is None:
        return None
    candidate = raw_category.strip()
    if candidate in categories:
        return candidate
    head = re.split(r"[:\-—]", candidate, maxsplit=1)[0].strip()
    if head in categories:
        return head
    logger.warning(
        "LLM вернула нераспознанный код категории %r (нет в config/categories.yaml) — "
        "считаем, что категория не определена",
        raw_category,
    )
    return None


def extract_legal_name(site_text: str) -> str | None:
    """Запасной вариант к regex-извлечению названия юрлица (sources/base.py
    LEGAL_ENTITY_RE) — см. pipeline._attach_legal_name, вызывается только
    если regex не нашёл название на странице. None — LLM недоступна/упала,
    или названия юрлица на странице правда нет; в обоих случаях вызывающий
    код просто не пробует повторный резолвинг в Dadata (см.
    enrichment.Enricher.re_resolve), компания остаётся как есть."""
    try:
        from procurement_search.yandexgpt_classifier import (
            extract_legal_name_with_yandexgpt as extract_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — LLM-извлечение названия юрлица пропущено")
        return None

    try:
        result = extract_fn(site_text)
    except Exception:
        logger.warning("LLM-извлечение названия юрлица не сработало", exc_info=True)
        return None
    return result.legal_name


def extract_contacts(site_text: str) -> tuple[str | None, str | None, str | None] | None:
    """Запасной вариант к regex-извлечению контактов (sources/base.py
    PHONE_RE/EMAIL_RE/ADDRESS_RE) — см. pipeline._attach_site_contacts,
    вызывается только на поля, которые regex не нашёл. В отличие от единиц
    измерения (attribute_extractor.py) у контактов нет курируемого словаря
    форматов — их пишут как угодно, поэтому здесь LLM может найти то, что
    не покрыл ни один вариант паттерна.

    Возвращает (phone, email, address) — любой элемент может быть None,
    если LLM тоже не нашла это поле. None целиком — LLM недоступна/упала,
    как и остальной Слой 3: вызывающий код трактует это как "нет сигнала",
    а не как "контактов точно нет"."""
    try:
        from procurement_search.yandexgpt_classifier import (
            extract_contacts_with_yandexgpt as extract_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — LLM-извлечение контактов (Слой 3) пропущено")
        return None

    try:
        result = extract_fn(site_text)
    except Exception:
        logger.warning("LLM-извлечение контактов не сработало", exc_info=True)
        return None
    return result.phone, result.email, result.address
