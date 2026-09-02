"""Слой 0 пайплайна: извлечение технических атрибутов из запроса байера.

Задача — отделить "2,5" в "лампочки светодиодные 2,5 квт" от обычных слов
названия товара: это не токен для нечёткого совпадения по тексту, а
конкретная характеристика (значение + единица измерения), которую байер
указал не просто так. scoring.py и site_relevance.py считают релевантность
через пересечение токенов — то же и составит будущий более весомый сигнал
"параметр из запроса нашёлся на сайте кандидата".

Основной путь — детерминированный: словарь единиц измерения из
config/units.yaml (данные, не код) плюс regex "число + единица рядом". LLM
(через yandexgpt_classifier.py) подключается только как fallback для
"голых" чисел, для которых рядом не нашлось известной единицы — и даже
тогда лишь выбирает единицу из уже существующего словаря, не выдумывая
новую. LLM-путь выключен по умолчанию
(use_llm_fallback=False).
"""

from __future__ import annotations

import logging
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
    # Спаны (start, end) в raw_query всех распознанных атрибутов (словарных
    # и LLM-подтверждённых) — extract_attributes уже считает их для
    # clean_text, здесь просто отдаём наружу: classify_roles использует их,
    # чтобы отличить "число уже понято как характеристика" от "голого"
    # числа, не дублируя ту же логику матчинга второй раз.
    matched_spans: list[tuple[int, int]] = field(default_factory=list)


@dataclass(frozen=True)
class Quantity:
    """Число с единицей и дословным текстом — тот же смысл, что у
    ExtractedAttribute (value/unit/raw), но с value: float (не строкой) —
    это публичный контракт для роль-классификации (classify_roles) и
    availability.py/quantity_match.py, где число сразу участвует в
    арифметике (сравнение "хватает ли остатка"), а не только в текстовом
    сравнении атрибутов, как ExtractedAttribute в scoring.py."""

    value: float
    unit: str  # канонический код из units.yaml, нижний регистр — как везде в проекте
    raw: str  # дословный текст из запроса/страницы, для отладки/вырезания


@dataclass
class ParsedQuery:
    """Результат роль-классификации чисел запроса (см. classify_roles) —
    Слой 0.6: что из запроса — характеристика товара (specs), что —
    количество ЗАКУПКИ (order_qty, не должно уходить в поисковый запрос,
    см. pipeline.py), что — индекс модели, и что осталось неоднозначным
    (conflicts, показывается байеру, а не разрешается молча).

    order_qty (штуки/комплекты/упаковки/пары) и order_length (метраж —
    метры/см/мм/км) — оба "сколько закупить", просто в разных единицах, для
    разных категорий товара (штучный товар vs. товар, который продаётся на
    отрез — кабель, трубы, шланги, ткань). Независимые поля, не одно и то
    же: запрос почти всегда даёт роль только одному из них (см. classify_roles
    про эвристику выбора), но оба одновременно возможны, если у каждого
    отдельно есть явный маркер количества в разных частях запроса
    (например "нужно 6 комплектов, требуется 50 метров кабеля к ним")."""

    product: str
    brand: str | None
    model: str | None
    specs: dict[str, Quantity]  # ключ — код единицы (units.yaml), не семантическое имя —
    # единственный стабильный идентификатор в проекте (см. scoring.has_attribute_mismatch,
    # тот же принцип: единица определяет характеристику, а не выдуманное название)
    order_qty: Quantity | None
    order_length: Quantity | None
    conflicts: list[str]


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
    return ExtractionResult(
        raw_query=raw_query, attributes=attributes, clean_text=clean_text, matched_spans=matched_spans
    )


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
    """Обёртка над classify_attributes_batch_with_yandexgpt с изоляцией
    сбоев — см. query_normalizer._try_llm_fallback про тот же приём и его
    мотивацию. Один batch-вызов на все naked_numbers разом, а не по вызову
    на число — см. докстринг extract_attributes."""
    try:
        from procurement_search.yandexgpt_classifier import (
            classify_attributes_batch_with_yandexgpt as classify_fn,
        )
    except ImportError:
        logger.warning("Пакет openai не установлен — LLM-fallback пропущен")
        return []

    try:
        guess = classify_fn(raw_query, naked_numbers, units)
    except Exception:
        logger.warning(
            "LLM-fallback атрибутов %r в запросе %r не сработал",
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


# --- Роль-классификация (Слой 0.6, см. ParsedQuery) ---

# Счётные единицы закупки — число с такой единицей это КОЛИЧЕСТВО, а не
# характеристика товара (см. units.yaml, группа "Счётные единицы закупки").
COUNT_UNITS = frozenset({"шт", "компл", "упак", "пара"})

# Единицы длины — число с такой единицей МОЖЕТ быть метражом закупки
# (order_length, "нужно 50 метров кабеля"), а может быть характеристикой
# ОДНОГО экземпляра товара (specs, "труба 1.5 метра, 20 штук" — тут 1.5
# метра — длина ОДНОЙ трубы). classify_roles различает эти два случая
# маркером/эвристикой, см. её докстринг — в отличие от COUNT_UNITS, здесь
# нет безусловного правила "единица -> роль", поэтому вынесено отдельным
# шагом, не в тот же словарный проход, что COUNT_UNITS.
LENGTH_UNITS = frozenset({"м", "мм", "см", "км"})

# Маркеры количества закупки — общие для order_qty (голые числа без
# единицы, "нужно 5 компрессоров") и order_length (числа с единицей длины,
# "нужно 50 метров кабеля") — то же слово выражает тот же смысл "хочу
# купить именно столько", независимо от того, что считается: штуки или
# метры. "×"/"x" из исходной постановки задачи сюда сознательно НЕ
# включены — они ложно совпадают с нотацией сечения кабеля ("3х2,5"), см.
# _CABLE_CSA_RE ниже и тест-кейс "кабель ВВГ 3х2,5 500 метров" (order_qty
# там должен остаться пустым).
_ORDER_AMOUNT_MARKERS = ("нужно", "требуется", "необходимо", "в количестве")

# Индекс модели — латинский токен (бренд) сразу перед голым числом без
# единицы ("hunter 3000"). Работает независимо от brand_extractor.py (тот
# требует LLM, этот — чистый regex): даже без LLM-извлечения бренда индекс
# модели детерминированно находится по самому запросу.
_LATIN_MODEL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9\-]{2,})\s+(\d+(?:[.,]\d+)?)\b")

# Сечение кабеля ("3х2,5", "3x2,5") — узкий спецкейс до основного прохода:
# обе цифры здесь не количество и не голое число для LLM-фоллбека, а одна
# характеристика (сечение), записанная через "х"/"x" как разделитель, а не
# единицу измерения. Без этого "3" и "2,5" ушли бы как два независимых
# "голых" числа и первое могло бы ложно попасть под маркер количества/
# индекс модели.
_CABLE_CSA_RE = re.compile(r"\b(\d+)\s*[xх×]\s*(\d+(?:[.,]\d+)?)\b", re.IGNORECASE)

_NUMBER_RE = re.compile(_NUMBER)


def _span_covered(span: tuple[int, int], covered: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(a <= start and end <= b for a, b in covered)


def _order_qty_from_marker(
    raw_query: str, covered_spans: list[tuple[int, int]]
) -> tuple[float, str] | None:
    """Первое "голое" число (не попавшее в covered_spans) в пределах ~20
    символов после одного из _ORDER_AMOUNT_MARKERS. Возвращает (value, raw)
    или None, если ни один маркер не дал числа."""
    lowered = raw_query.lower()
    for marker in _ORDER_AMOUNT_MARKERS:
        idx = lowered.find(marker)
        if idx == -1:
            continue
        window_start = idx + len(marker)
        window = raw_query[window_start : window_start + 20]
        m = _NUMBER_RE.search(window)
        if m is None:
            continue
        span = (window_start + m.start(), window_start + m.end())
        if _span_covered(span, covered_spans):
            continue
        return float(m.group(0).replace(",", ".")), m.group(0)
    return None


def _marker_immediately_before(raw_query: str, raw_text: str, markers: tuple[str, ...]) -> bool:
    """Есть ли один из markers в пределах ~20 символов ПЕРЕД первым
    вхождением raw_text в raw_query — для уже найденного словарного
    совпадения (число+единица длины, см. LENGTH_UNITS в classify_roles),
    в отличие от _order_qty_from_marker (тот ищет голое число ПОСЛЕ
    маркера, потому что у голого числа своей позиции ещё не известно).
    Ищет по подстроке `raw_text`, а не по позиции в matched_spans —
    та коллекция не гарантированно выровнена 1:1 с extraction.attributes,
    если LLM-fallback вернул фразу, которую не удалось найти дословно
    (см. extract_attributes/_phrase_spans)."""
    idx = raw_query.lower().find(raw_text.lower())
    if idx == -1:
        return False
    window_start = max(0, idx - 20)
    window = raw_query[window_start:idx].lower()
    return any(marker in window for marker in markers)


def _numbers_match(value: float, text: str) -> bool:
    try:
        return value == float(text.replace(",", "."))
    except ValueError:
        return False


def _guess_spec_category(product_text: str, spec_ranges: dict) -> str | None:
    """Первое категория из spec_ranges.yaml, чьи keywords встречаются в
    product_text — словарное совпадение, не LLM (см. докстринг
    config/spec_ranges.yaml). Порядок совпадения — порядок ключей в YAML;
    отсутствие совпадения — не ошибка, просто нет категории для проверки
    диапазонов (тот же принцип "нет данных — не штраф", что везде в
    scoring.py)."""
    lowered = product_text.lower()
    for category, definition in spec_ranges.items():
        keywords = definition.get("keywords", [])
        if any(kw.lower() in lowered for kw in keywords):
            return category
    return None


def _remove_substring(text: str, substring: str) -> str:
    """Убирает первое вхождение substring из text дословно и схлопывает
    пробелы — тот же приём, что _strip_spans, но по подстроке, а не по
    индексам (нужно, когда substring найден внутри уже пересчитанного
    clean_text, а не исходного raw_query, см. classify_roles про
    order_qty из маркерного прохода)."""
    idx = text.find(substring)
    if idx == -1:
        return text
    return re.sub(r"\s+", " ", text[:idx] + text[idx + len(substring) :]).strip()


def classify_roles(
    extraction: ExtractionResult,
    raw_query: str,
    brand: str | None,
    units: dict,
    spec_ranges: dict | None = None,
) -> ParsedQuery:
    """Слой 0.6: классифицирует числа уже готового ExtractionResult по
    ролям — количество закупки / характеристика / индекс модели / неясно.
    Чистый пост-процессинг, без отдельного LLM-вызова (extraction уже мог
    использовать LLM-fallback для "голых" чисел, здесь это не повторяется).

    Порядок применения правил (по приоритету, см. ТЗ):
      1. Счётная единица (шт/компл/упак/пара) -> order_qty; единица длины
         (LENGTH_UNITS) откладывается отдельно (кандидат в order_length,
         см. п.2.5); прочая физическая единица -> specs.
      2. (см. выше, тот же проход).
      2.5. Метраж закупки (order_length) — см. LENGTH_UNITS и её докстринг:
         явный маркер количества рядом с конкретным вхождением побеждает
         независимо от остального; без маркера — единственная во всём
         запросе величина длины, при отсутствии order_qty, тоже считается
         метражом закупки (типовой случай "кабель 3х2,5 500 метров"); все
         остальные длины уходят в specs как обычные характеристики
         (типовой случай "труба 1.5 метра, 20 штук" — order_qty уже занят
         шагом 1, поэтому 1.5 метра остаётся длиной ОДНОЙ трубы).
      3. Маркер количества над "голым" числом -> order_qty (если ещё не занят).
      4. Латинский бренд + голое число без единицы -> model (независимо от 1-3 —
         то же число может одновременно попасть в specs И стать индексом модели,
         это и есть коллизия, которую нужно показать в conflicts, а не скрыть).
      5. Диапазоны типичности (spec_ranges) -> conflicts, если значение specs
         вне диапазона категории; если совпадает с моделью — уточняющая фраза.

    `product` — clean_text с дополнительно вырезанным order_qty/order_length,
    если они найдены только маркерным проходом (словарные order_qty/
    order_length уже отсутствуют в clean_text, см. extract_attributes:
    matched_spans покрывает все dict-совпадения, не только специфики)."""
    units = units if units is not None else {}
    covered_spans = list(extraction.matched_spans)

    # 0. Сечение кабеля — до всего остального (см. _CABLE_CSA_RE).
    specs: dict[str, Quantity] = {}
    cable_match = _CABLE_CSA_RE.search(raw_query)
    if cable_match and "мм2" in units:
        csa_value = float(cable_match.group(2).replace(",", "."))
        specs["мм2"] = Quantity(csa_value, "мм2", cable_match.group(0))
        covered_spans.append(cable_match.span())

    # 1. Словарные атрибуты: счётная единица -> order_qty, единица длины ->
    # отдельный список кандидатов (см. п.2.5), иначе -> specs.
    order_qty: Quantity | None = None
    length_candidates: list[Quantity] = []
    conflicts: list[str] = []
    for attr in extraction.attributes:
        value = float(attr.value)
        if attr.unit in COUNT_UNITS:
            if order_qty is None:
                order_qty = Quantity(value, attr.unit, attr.raw_text)
            else:
                conflicts.append(
                    f"В запросе похоже несколько разных количеств закупки: "
                    f"{order_qty.raw!r} и {attr.raw_text!r} — уточните, какое верное."
                )
        elif attr.unit in LENGTH_UNITS:
            length_candidates.append(Quantity(value, attr.unit, attr.raw_text))
        elif attr.unit not in specs:
            specs[attr.unit] = Quantity(value, attr.unit, attr.raw_text)

    # 2.5. Метраж закупки — см. докстринг метода выше про приоритет
    # "маркер побеждает, иначе — единственная длина без order_qty".
    order_length: Quantity | None = None
    marked_lengths = [
        q for q in length_candidates if _marker_immediately_before(raw_query, q.raw, _ORDER_AMOUNT_MARKERS)
    ]
    if marked_lengths:
        order_length = marked_lengths[0]
        if len(marked_lengths) > 1:
            conflicts.append(
                f"В запросе похоже несколько разных запрошенных метражей: "
                f"{marked_lengths[0].raw!r} и {marked_lengths[1].raw!r} — уточните, какой верный."
            )
    elif order_qty is None and len(length_candidates) == 1:
        order_length = length_candidates[0]

    for q in length_candidates:
        if (order_length is None or q.raw != order_length.raw) and q.unit not in specs:
            specs[q.unit] = q

    # 3. Маркер количества над "голым" числом.
    if order_qty is None:
        marker_result = _order_qty_from_marker(raw_query, covered_spans)
        if marker_result is not None:
            value, raw_text = marker_result
            order_qty = Quantity(value, "компл", raw_text)

    # 4. Индекс модели — независимо от 1-3, включая уже занятые specs числа.
    model: str | None = None
    model_match = _LATIN_MODEL_RE.search(raw_query)
    if model_match:
        model = model_match.group(2)

    # 5. Диапазоны типичности.
    if spec_ranges:
        category = _guess_spec_category(extraction.clean_text, spec_ranges)
        if category is not None:
            ranges = spec_ranges[category].get("ranges", {})
            category_name = spec_ranges[category].get("name", category)
            for unit_code, qty in specs.items():
                range_def = ranges.get(unit_code)
                if range_def is None:
                    continue
                if range_def["min"] <= qty.value <= range_def["max"]:
                    continue
                label = range_def.get("label", unit_code)
                msg = (
                    f"{qty.raw!r} нетипично для категории «{category_name}» "
                    f"(обычный диапазон «{label}»: {range_def['min']}–{range_def['max']} {unit_code})."
                )
                if model is not None and _numbers_match(qty.value, model):
                    brand_part = f"{brand} " if brand else ""
                    msg += f" Это {label} или индекс модели {brand_part}{model}?"
                conflicts.append(msg)

    # 6. product — clean_text без order_qty/order_length, если они остались
    # в нём. Для order_qty это актуально только в маркерном случае
    # (словарный order_qty уже отсутствует в clean_text). Для order_length
    # словарный случай тоже уже отсутствует — вырезание здесь чисто
    # защитное, на случай редкого LLM-fallback пути, где фраза не нашлась
    # дословно при построении matched_spans (см. extract_attributes/
    # _phrase_spans) и не попала в clean_text автоматически.
    product = extraction.clean_text
    if order_qty is not None:
        product = _remove_substring(product, order_qty.raw)
    if order_length is not None:
        product = _remove_substring(product, order_length.raw)

    return ParsedQuery(
        product=product,
        brand=brand,
        model=model,
        specs=specs,
        order_qty=order_qty,
        order_length=order_length,
        conflicts=conflicts,
    )


def parse_query_roles(
    raw_query: str,
    units: dict | None = None,
    spec_ranges: dict | None = None,
    use_llm_fallback: bool = False,
) -> ParsedQuery:
    """Тонкая обёртка для автономного использования/тестов — extract_attributes
    + brand_extractor.extract_brand + classify_roles одним вызовом.

    pipeline.py её НЕ вызывает: там extract_attributes/extract_brand уже
    вызываются по одному разу для других нужд (Слой 0.5/бренд-термин
    поиска), повторный вызов здесь удвоил бы LLM-запросы при
    use_llm_fallback=True."""
    from procurement_search.brand_extractor import extract_brand

    units = units if units is not None else load_units()
    extraction = extract_attributes(raw_query, units=units, use_llm_fallback=use_llm_fallback)
    brand = extract_brand(raw_query, use_llm_fallback=use_llm_fallback)
    return classify_roles(extraction, raw_query, brand, units, spec_ranges=spec_ranges)
