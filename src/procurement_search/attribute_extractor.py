"""Слой 0 пайплайна: извлечение технических атрибутов из запроса байера.

Задача — отделить "2,5" в "лампочки светодиодные 2,5 квт" от обычных слов
названия товара: это не токен для нечёткого совпадения по тексту, а
конкретная характеристика (значение + единица измерения), которую байер
указал не просто так. scoring.py и site_relevance.py считают релевантность
через пересечение токенов — то же и составит будущий более весомый сигнал
"параметр из запроса нашёлся на сайте кандидата".

Основной путь — детерминированный: словарь единиц измерения из
config/units.yaml (данные, не код) плюс regex "число + единица рядом". LLM
(через yandexgpt_classifier.py/cloudru_classifier.py, выбор — LLM_PROVIDER)
подключается только как fallback для "голых" чисел, для которых рядом не нашлось
известной единицы — и даже тогда лишь выбирает единицу из уже
существующего словаря, не выдумывая новую. LLM-путь выключен по умолчанию
(use_llm_fallback=False).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from procurement_search.config import load_units

logger = logging.getLogger(__name__)


@dataclass
class ExtractedAttribute:
    value: str  # нормализовано: "," -> "." (2,5 -> 2.5)
    unit: str  # канонический код из units.yaml (ключ верхнего уровня)
    raw_text: str  # как было в запросе, для отладки/отображения байеру
    source: str = "dict"  # "dict" | "llm" — откуда взялось значение unit


@dataclass
class ExtractionResult:
    raw_query: str
    attributes: list[ExtractedAttribute] = field(default_factory=list)
    clean_text: str = ""  # запрос без спанов атрибутов — чистое "название товара"


_NUMBER = r"\d+(?:[.,]\d+)?"

# ГОСТ-нотация трубопроводной арматуры пишет единицу ПЕРЕД числом, слитно:
# "Ду50", "Ру16" (условный проход/условное давление), а не "50 ду" как
# обычные метрические характеристики. Список — намеренно узкий allowlist,
# а не общее правило "любая единица может стоять и до, и после числа":
# для многобуквенных алиасов вроде "м" это дало бы ложные срабатывания
# (маркировка резьбы "М10" — это не "10 метров").
_UNIT_FIRST_UNITS = frozenset({"ду", "ру"})


def _build_alias_pattern(units: dict) -> tuple[re.Pattern, re.Pattern | None, dict[str, str]]:
    """Строит общий regex на все алиасы всех единиц разом (число -> единица)
    и отдельный, для узкого набора _UNIT_FIRST_UNITS (единица -> число).

    Алиасы отсортированы по длине по убыванию: без этого при альтернации
    "квт" мог бы перехватить совпадение раньше более длинного "квт*ч" на
    той же позиции, и "5 квт*ч" (киловатт-часы) тихо превратилось бы в
    "5 квт" (киловатты) — единица есть, но не та, что имел в виду байер.
    """
    alias_to_unit: dict[str, str] = {}
    for unit_code, aliases in units.items():
        for alias in aliases:
            alias_to_unit[alias.lower()] = unit_code

    aliases_sorted = sorted(alias_to_unit, key=len, reverse=True)
    alternation = "|".join(re.escape(a) for a in aliases_sorted)
    pattern = re.compile(
        rf"(?P<value>{_NUMBER})\s*(?P<unit>{alternation})\b",
        re.IGNORECASE,
    )

    unit_first_aliases = sorted(
        (a for a in aliases_sorted if alias_to_unit[a] in _UNIT_FIRST_UNITS), key=len, reverse=True
    )
    unit_first_pattern = None
    if unit_first_aliases:
        unit_first_alternation = "|".join(re.escape(a) for a in unit_first_aliases)
        unit_first_pattern = re.compile(
            rf"\b(?P<unit>{unit_first_alternation})\s*(?P<value>{_NUMBER})",
            re.IGNORECASE,
        )
    return pattern, unit_first_pattern, alias_to_unit


def extract_attributes(
    raw_query: str,
    units: dict | None = None,
    use_llm_fallback: bool = False,
) -> ExtractionResult:
    """Извлекает пары значение+единица из запроса байера.

    Матчинг по пересечению "число рядом с известным алиасом единицы" —
    простой и прозрачный, при этом достаточный для конечного,
    курируемого человеком словаря единиц (config/units.yaml).

    Если `use_llm_fallback=True` и в запросе остались "голые" числа, для
    которых рядом не нашлось известного алиаса — просит LLM (по умолчанию
    yandexgpt_classifier.classify_attributes_batch_with_yandexgpt) одним
    batch-вызовом подобрать единицы для ВСЕХ таких чисел разом, с реальными
    алиасами из словаря в промпте — это ловит не только "голые" числа без
    единицы вообще, но и опечатки/сокращения/склонения в написании самой
    единицы, которые словарный regex не распознал (например, "киловат"
    вместо "киловатт" — обычный regex-проход это не поймает, а LLM с
    полным словарём алиасов перед глазами — поймает). Требует переменных
    окружения выбранного провайдера (см. _try_llm_fallback); при сетевой/
    API-ошибке откатывается на "у этих чисел нет единицы", не роняя весь
    пайплайн.
    """
    units = units if units is not None else load_units()
    pattern, unit_first_pattern, alias_to_unit = _build_alias_pattern(units)

    attributes: list[ExtractedAttribute] = []
    matched_spans: list[tuple[int, int]] = []
    for m in pattern.finditer(raw_query):
        canonical = alias_to_unit[m.group("unit").lower()]
        attributes.append(
            ExtractedAttribute(
                value=m.group("value").replace(",", "."),
                unit=canonical,
                raw_text=m.group(0),
                source="dict",
            )
        )
        matched_spans.append(m.span())

    if unit_first_pattern is not None:
        for m in unit_first_pattern.finditer(raw_query):
            if any(a <= m.start() and m.end() <= b for a, b in matched_spans):
                continue  # уже покрыто числом-впереди-единицы выше
            canonical = alias_to_unit[m.group("unit").lower()]
            attributes.append(
                ExtractedAttribute(
                    value=m.group("value").replace(",", "."),
                    unit=canonical,
                    raw_text=m.group(0),
                    source="dict",
                )
            )
            matched_spans.append(m.span())

    naked_numbers = _find_naked_numbers(raw_query, matched_spans)
    if naked_numbers and use_llm_fallback:
        llm_attributes = _try_llm_fallback(raw_query, naked_numbers, units)
        attributes.extend(llm_attributes)
        # То, что LLM успешно распознала (число целиком вместе со словом
        # единицы, если оно было рядом), тоже вырезаем из clean_text —
        # иначе оно останется висеть в "названии товара". raw_text ищем
        # дословной подстрокой, не только по числу (см. _phrase_spans) —
        # LLM могла вернуть фразу шире одного числа ("5,5 киловат").
        matched_spans.extend(_phrase_spans(raw_query, [a.raw_text for a in llm_attributes]))

    clean_text = _strip_spans(raw_query, matched_spans)
    return ExtractionResult(raw_query=raw_query, attributes=attributes, clean_text=clean_text)


def _find_naked_numbers(raw_query: str, matched_spans: list[tuple[int, int]]) -> list[str]:
    """Числа в запросе, не попавшие ни в один матч 'число+единица' —
    кандидаты на LLM-fallback (или просто количество/артикул без единицы,
    LLM решит и это тоже, вернув unit: null)."""
    naked = []
    for m in re.finditer(_NUMBER, raw_query):
        if not any(a <= m.start() and m.end() <= b for a, b in matched_spans):
            naked.append(m.group(0))
    return naked


def _phrase_spans(raw_query: str, phrases: list[str]) -> list[tuple[int, int]]:
    """Ищет каждую фразу как дословную подстроку raw_query (не только
    число — LLM может вернуть raw шире одного числа, например "5,5
    киловат"). Фраза, которую не удалось найти дословно (например, LLM
    слегка переформулировала её вопреки инструкции) — просто пропускается,
    не вырезается из clean_text: лучше оставить лишнее слово в названии
    товара, чем упасть или испортить текст произвольным вырезанием."""
    spans = []
    search_from = 0
    for phrase in phrases:
        idx = raw_query.find(phrase, search_from)
        if idx == -1:
            logger.warning(
                "LLM-фраза %r не найдена дословно в запросе %r — не вырезаем из clean_text",
                phrase,
                raw_query,
            )
            continue
        spans.append((idx, idx + len(phrase)))
        search_from = idx + len(phrase)
    return spans


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    spans = sorted(spans)
    parts = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _try_llm_fallback(
    raw_query: str, naked_numbers: list[str], units: dict[str, list[str]]
) -> list[ExtractedAttribute]:
    """Обёртка над classify_attributes_batch_with_yandexgpt/_cloudru с
    изоляцией сбоев — см. query_normalizer._try_llm_fallback про тот же
    приём и его мотивацию. Один batch-вызов на все naked_numbers разом,
    а не по вызову на число — см. докстринг extract_attributes."""
    provider = os.environ.get("LLM_PROVIDER", "yandexgpt")
    try:
        if provider == "cloudru":
            from procurement_search.cloudru_classifier import (
                classify_attributes_batch_with_cloudru as classify_fn,
            )
        else:
            from procurement_search.yandexgpt_classifier import (
                classify_attributes_batch_with_yandexgpt as classify_fn,
            )
    except ImportError:
        logger.warning("Пакет для провайдера %r не установлен — LLM-fallback пропущен", provider)
        return []

    try:
        guess = classify_fn(raw_query, naked_numbers, units)
    except Exception:
        logger.warning(
            "LLM-fallback (%s) атрибутов %r в запросе %r не сработал",
            provider,
            naked_numbers,
            raw_query,
            exc_info=True,
        )
        return []

    known_units = set(units.keys())
    naked_set = {n.replace(",", ".") for n in naked_numbers}
    results: list[ExtractedAttribute] = []
    for item in guess.attributes:
        if item.unit is None:
            continue
        if item.unit not in known_units:
            # Той же дисциплины, что и в query_normalizer: не доверяем
            # модели вслепую, единица обязана быть из уже существующего
            # словаря (config/units.yaml), а не придумана на лету.
            logger.warning("LLM вернула единицу %r вне словаря — игнорируем", item.unit)
            continue

        value = item.value.replace(",", ".")
        if value not in naked_set:
            # Модель могла вернуть значение, не входившее в исходный
            # список "голых" чисел (перепутала/додумала) — не доверяем,
            # включаем только то, о чём реально спрашивали.
            logger.warning(
                "LLM вернула значение %r, не входящее в список голых чисел %r — игнорируем",
                item.value,
                naked_numbers,
            )
            continue

        results.append(
            ExtractedAttribute(value=value, unit=item.unit, raw_text=item.raw, source="llm")
        )
    return results
