"""Слой 0 пайплайна: извлечение технических атрибутов из запроса байера.

Задача — отделить "2,5" в "лампочки светодиодные 2,5 квт" от обычных слов
названия товара: это не токен для нечёткого совпадения по тексту, а
конкретная характеристика (значение + единица измерения), которую байер
указал не просто так. scoring.py и site_relevance.py считают релевантность
через пересечение токенов — то же и составит будущий более весомый сигнал
"параметр из запроса нашёлся на сайте кандидата".

Основной путь — детерминированный: словарь единиц измерения из
config/units.yaml (данные, не код, ровно тот же принцип, что и у
categories.yaml) плюс regex "число + единица рядом". LLM (через
llm_classifier.py/ollama_classifier.py, выбор — LLM_PROVIDER) подключается
только как fallback для "голых" чисел, для которых рядом не нашлось
известной единицы — и даже тогда лишь выбирает единицу из уже
существующего словаря, не выдумывая новую. LLM-путь выключен по умолчанию
(use_llm_fallback=False), см. query_normalizer.py про то же самое решение
и его мотивацию.
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
    которых рядом не нашлось известного алиаса — просит LLM
    (llm_classifier.classify_attribute_with_llm) подобрать единицу из
    того же словаря. Требует ANTHROPIC_API_KEY (или LLM_PROVIDER=ollama);
    при сетевой/API-ошибке откатывается на "у этого числа нет единицы",
    не роняя весь пайплайн.
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
        llm_attributes = _try_llm_fallback(raw_query, naked_numbers, list(units.keys()))
        attributes.extend(llm_attributes)
        # Число, которое LLM успешно распознала, тоже вырезаем из
        # clean_text — иначе оно останется висеть в "названии товара".
        matched_spans.extend(_number_spans(raw_query, [a.raw_text for a in llm_attributes]))

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


def _number_spans(raw_query: str, numbers: list[str]) -> list[tuple[int, int]]:
    spans = []
    search_from = 0
    for number in numbers:
        idx = raw_query.index(number, search_from)
        spans.append((idx, idx + len(number)))
        search_from = idx + len(number)
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
    raw_query: str, naked_numbers: list[str], known_units: list[str]
) -> list[ExtractedAttribute]:
    """Обёртка над classify_attribute_with_llm/_ollama с изоляцией сбоев —
    см. query_normalizer._try_llm_fallback про тот же приём и его мотивацию.
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    try:
        if provider == "ollama":
            from procurement_search.ollama_classifier import (
                classify_attribute_with_ollama as classify_fn,
            )
        else:
            from procurement_search.llm_classifier import (
                classify_attribute_with_llm as classify_fn,
            )
    except ImportError:
        logger.warning("Пакет для провайдера %r не установлен — LLM-fallback пропущен", provider)
        return []

    results: list[ExtractedAttribute] = []
    for number in naked_numbers:
        try:
            guess = classify_fn(raw_query, number, known_units)
        except Exception:
            logger.warning(
                "LLM-fallback (%s) атрибута %r в запросе %r не сработал",
                provider,
                number,
                raw_query,
                exc_info=True,
            )
            continue

        if guess.unit is None:
            continue
        if guess.unit not in known_units:
            # Той же дисциплины, что и в query_normalizer: не доверяем
            # модели вслепую, единица обязана быть из уже существующего
            # словаря (config/units.yaml), а не придумана на лету.
            logger.warning("LLM вернула единицу %r вне словаря — игнорируем", guess.unit)
            continue

        results.append(
            ExtractedAttribute(value=number.replace(",", "."), unit=guess.unit, raw_text=number, source="llm")
        )
    return results
