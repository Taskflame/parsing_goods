"""Шаг [6] пайплайна: скоринг.

Три независимых числа — relevance / trust / confidence — а не одна
взвешенная сумма. Причина: сумма позволяет одному провалу вытягиваться за
счёт другого (крупная старая компания не по теме перебивает маленького
профильного поставщика). `compute_score` вместо этого:

  1. считает три оси отдельно, каждая в [0, 1];
  2. перемножает relevance^a × trust^b (умножение = "И", а не "ИЛИ" —
     нулевая релевантность обнуляет итог независимо от trust);
  3. домножает на мягкий штраф за неполноту (0.5 + 0.5×confidence по
     умолчанию) — карточка с одним только названием не исчезает из
     выдачи, но опускается вниз, а не выдаёт себя за проверенную.

`a`, `b`, база штрафа и веса под-сигналов trust — всё в
config/scoring_weights.yaml, с профилем на категорию (ТЗ требует
тиражируемости — для "лабораторных услуг" лицензия/аккредитация значат
больше, чем для остальных категорий, и это должно настраиваться без
правки кода).

`compute_relevance` — Слой 1 честной трёхслойной схемы уточнения
релевантности: пересечение токенов запроса с текстом сниппета из
поисковой выдачи (Candidate.description_raw) — дёшево, работает на всех
кандидатах сразу, без сети сверх того, что источники уже сделали. Слои
2 (краулинг сайта кандидата + сравнение по реальному тексту) и 3
(точечная LLM-проверка) — в site_relevance.py / relevance_llm.py,
применяются только к top-N после этого грубого прохода (см. pipeline.py).

`compute_trust` — сигналы о самой компании (жива ли, резолвится ли в
ЕГРЮЛ, есть ли сайт). Большая часть таблицы сигналов из design-обсуждения
(выручка/численность/ФССП/КАД/лицензии) требует источников данных, которых
в прототипе ещё нет (ФНС открытые данные, ФССП, КАД, реестры лицензий) —
сигналы под них уже заведены в `_TRUST_SIGNAL_ORDER` и молчат, пока
соответствующие поля Company не начнёт кто-то заполнять; это не бага, а
честное "нет данных = не считаем это плюсом", как и раньше.
"""

from __future__ import annotations

import math
import re

import snowballstemmer

from procurement_search.attribute_extractor import ExtractedAttribute, extract_attributes
from procurement_search.config import load_scoring_weights
from procurement_search.models import Company, FieldValue, ScoreBreakdown, VerificationFlag
from procurement_search.query_normalizer import NormalizedQuery

# Насколько может отличаться значение атрибута на сайте кандидата от
# запрошенного, прежде чем это считается несоответствием, а не разумной
# вариацией ("лучше — не хуже", design-обсуждение: 12000 л/ч на сайте для
# запроса "10000 л/час" — это НЕ несоответствие, ratio=1.2; а 18 л/ч —
# несоответствие, ratio=0.0018). Симметричный множитель: за пределами
# [1/x, x] от запрошенного значения.
_ATTRIBUTE_MISMATCH_RATIO = 2.0

# Порядок альтернатив важен: дробное число (напр. "2,5") должно матчиться
# раньше отдельных цифр, иначе "2,5 квт" разваливается на токены "2" и "5" —
# два несвязанных однозначных числа, которые совпадают почти с чем угодно
# (телефоны, годы, другие мощности) и на практике убивают сигнал атрибута.
_TOKEN_RE = re.compile(r"[а-яa-zё]+|\d+[.,]\d+|\d+", re.IGNORECASE)

# Стемминг русских словоформ ("частотный" -> "частотн", "частоты" -> "частот")
# — без него токенное пересечение сравнивает точные строки, а склонение
# делает "преобразователь"/"преобразователи" разными токенами (design-
# обсуждение: реальный кейс, где карточка с полным текстовым совпадением
# по смыслу получала relevance около нуля просто из-за формы слова).
# snowballstemmer — чистый Python, без словарей/компиляции, ровно для этой
# задачи. НЕ лемматизация: склеивает только словоизменение одного слова,
# не объединяет однокоренные, но грамматически разные слова ("частотный" —
# прилагательное, "частоты" — форма другого слова "частота"; это разная
# лексика с общим смыслом, стемминг такое не унифицирует).
_RU_STEMMER = snowballstemmer.stemmer("russian")

# Порядок и веса по умолчанию для сигналов trust — переопределяются per-
# категорийно через scoring_weights.yaml -> <категория>.trust_signal_weights.
# Каждый сигнал нормирован в [0, 1]; отсутствующий сигнал = None, а не 0 —
# см. _weighted_average про то, почему это принципиально.
_DEFAULT_TRUST_SIGNAL_WEIGHTS = {
    "status": 0.35,
    "website_alive": 0.15,
    "inn_resolved": 0.15,
    "years_in_business": 0.15,
    "employees": 0.10,
    "revenue": 0.10,
}

_DEFAULT_SCORE_WEIGHTS = {
    "relevance_exponent": 0.6,
    "trust_exponent": 0.4,
    "confidence_base": 0.5,
    "trust_prior": 0.5,
    "trust_shrinkage": 0.3,
}

# Число полей карточки поставщика, по которым считается confidence (ТЗ п.4:
# "глубина проработки нестабильна — где-то только название, где-то контакты
# и контактное лицо"). Порядок не важен — важно только количество и то, что
# каждое поле реально отражает то, что байеру нужно в карточке.
_CONFIDENCE_FIELDS = 10


def _tokenize(text: str) -> set[str]:
    # "," -> "." после матчинга, чтобы "2,5" (запрос) и "2.5" (текст сайта)
    # считались одним и тем же токеном независимо от формата разделителя.
    raw_tokens = [t.lower().replace(",", ".") for t in _TOKEN_RE.findall(text)]
    # Числа не стеммируем (это операция над словами, не над цифрами) —
    # отделяем их от слов до вызова стеммера.
    words = [t for t in raw_tokens if not t[0].isdigit()]
    numbers = [t for t in raw_tokens if t[0].isdigit()]
    stemmed = _RU_STEMMER.stemWords(words) if words else []
    return set(stemmed) | set(numbers)


def query_tokens(normalized_query: NormalizedQuery, clean_query_text: str | None = None) -> set[str]:
    """Множество токенов запроса — общее для Слоя 1 (здесь) и Слоя 2
    (site_relevance.py), чтобы не дублировать токенизацию в двух местах.

    `clean_query_text` — результат attribute_extractor.extract_attributes(
    raw_query).clean_text (Слой 0): сырой запрос без чисел и слов единиц
    измерения. Если передан, подменяет собой raw_query (всегда первый
    элемент search_terms, см. query_normalizer.normalize_query) —
    остальные search_terms (категория, синонимы) не трогаем, они и так
    без чисел. Без этого голые числа запроса ("5.5", "380") сравниваются
    как обычные слова и совпадают почти с любым сайтом той же товарной
    группы (design-обсуждение) — числовое соответствие проверяется
    отдельно, в has_attribute_mismatch, где сравнивается СМЫСЛ значения
    (та же единица измерения), а не совпадение случайной цифры."""
    terms = list(normalized_query.search_terms)
    if clean_query_text is not None and terms:
        terms[0] = clean_query_text
    tokens: set[str] = set()
    for term in terms:
        tokens |= _tokenize(term)
    return tokens


def compute_relevance(
    company: Company, normalized_query: NormalizedQuery, clean_query_text: str | None = None
) -> float:
    """Слой 1: пересечение токенов запроса с текстом сниппетов кандидатов.
    Сниппет — то, что источник (Yandex/DDG/каталог) сам показал в выдаче,
    не текст с сайта компании — грубее Слоя 2, но бесплатно и мгновенно.

    `clean_query_text` — см. query_tokens."""
    q_tokens = query_tokens(normalized_query, clean_query_text=clean_query_text)
    if not q_tokens:
        return 0.0

    text_parts = [company.name.value]
    for candidate in company.raw_candidates:
        if candidate.description_raw:
            text_parts.append(candidate.description_raw)
    text_tokens = _tokenize(" ".join(text_parts))

    overlap = len(q_tokens & text_tokens)
    return min(1.0, overlap / len(q_tokens))


def _weighted_average(
    signals: list[tuple[float, float | None]], prior: float, shrinkage: float
) -> float:
    """Средневзвешенное только по найденным сигналам, веса перенормированы
    на их сумму — отсутствующий сигнал (None) не наказывается как 0,
    отсутствие данных о выручке не значит "плохая выручка".

    С усадкой к `prior` (типовая компания, по умолчанию 0.5): без неё один
    случайно найденный идеальный сигнал (например, только возраст домена)
    давал бы trust=1.0, а полное отсутствие данных — trust=0.0, обнуляя
    итоговый score целиком (0.0 в основании степени = 0 независимо от
    relevance) для КАЖДОЙ компании, пока не резолвится ЕГРЮЛ (то есть
    всегда без DADATA_API_KEY — это большинство реальных прогонов
    прототипа). Усадка добавляет фиктивный сигнал "типовая компания" с
    весом `shrinkage`: чем меньше реальных данных, тем сильнее тянет к
    нейтральной середине; чем больше данных — тем меньше поправка заметна."""
    total_weight = sum(w for w, v in signals if v is not None) + shrinkage
    weighted_sum = sum(w * v for w, v in signals if v is not None) + shrinkage * prior
    return weighted_sum / total_weight


def _status_signal(status: str) -> float | None:
    return {
        "действующая": 1.0,
        "в процессе реорганизации": 0.5,
        "в процессе ликвидации": 0.15,
        "ликвидирована": 0.0,
        # ликвидированные компании в норме не доходят сюда — knockout в
        # pipeline.py убирает их до скоринга; сигнал сохранён явно на
        # случай прямого вызова compute_score/compute_trust в обход
        # пайплайна (как в тестах). "неизвестно" -> None ниже (не в
        # словаре) - нет данных, не штраф.
    }.get(status)


def _numeric_field_signal(field: FieldValue | None, cap: float) -> float | None:
    """Линейная нормировка числового поля в [0, cap] -> [0, 1]. Линейная,
    не log-шкала — сознательное упрощение, пока это поле вообще никем не
    заполняется (ни один enricher не пишет employees_count/years_in_business,
    см. enrichment.py); когда появятся реальные данные, для выручки/
    численности разумнее log-шкала (см. _revenue_signal ниже) — тогда и
    имеет смысл различие."""
    if field is None:
        return None
    try:
        value = float(field.value)
    except (TypeError, ValueError):
        return None
    return min(max(value, 0.0) / cap, 1.0)


def _revenue_signal(revenue_history: list[FieldValue]) -> float | None:
    """Log-шкала — выручка растягивается на порядки, линейная нормировка
    сделала бы 10 млн и 500 млн почти неразличимыми. min(лет/10,1)-стиль
    тут не годится."""
    if not revenue_history:
        return None
    try:
        value = float(revenue_history[0].value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return 0.0
    return min(math.log10(value) / 9, 1.0)  # log10(1 млрд) = 9 -> потолок


def compute_trust(company: Company, weights: dict | None = None) -> float:
    """Trust: живое ли, резолвится ли в ЕГРЮЛ, отвечает ли сайт. `weights`
    — весь блок весов категории из scoring_weights.yaml (не только
    trust_signal_weights, но и trust_prior/trust_shrinkage — см.
    _weighted_average про усадку к среднему). См. докстринг модуля про то,
    каких сигналов здесь ещё нет (выручка/численность реально
    заполняются — сигналы заведены, но пока молчат; ФССП/КАД/лицензии —
    источников данных пока нет вообще, не заведены даже заглушками)."""
    weights = weights or {}
    signal_weights = weights.get("trust_signal_weights") or _DEFAULT_TRUST_SIGNAL_WEIGHTS
    prior = weights.get("trust_prior", _DEFAULT_SCORE_WEIGHTS["trust_prior"])
    shrinkage = weights.get("trust_shrinkage", _DEFAULT_SCORE_WEIGHTS["trust_shrinkage"])

    website_flags = [fv.confidence for fv in company.contacts.get("website", [])]
    website_alive = 1.0 if VerificationFlag.CONFIRMED in website_flags else None

    signals: list[tuple[float, float | None]] = [
        (signal_weights.get("status", 0.0), _status_signal(company.status)),
        (signal_weights.get("website_alive", 0.0), website_alive),
        (signal_weights.get("inn_resolved", 0.0), 1.0 if company.inn else 0.0),
        (signal_weights.get("years_in_business", 0.0), _numeric_field_signal(company.years_in_business, cap=10.0)),
        (signal_weights.get("employees", 0.0), _numeric_field_signal(company.employees_count, cap=250.0)),
        (signal_weights.get("revenue", 0.0), _revenue_signal(company.revenue_last_2y)),
    ]
    return _weighted_average(signals, prior=prior, shrinkage=shrinkage)


def compute_confidence(company: Company) -> float:
    """Чек-лист заполненности карточки — прямой ответ на ТЗ п.4
    ("глубина проработки нестабильна"). Не влияет на relevance/trust,
    показывается байеру отдельно (export.py), чтобы не путать "плохой
    поставщик" и "мало о нём известно" — см. докстринг модуля."""
    filled = [
        company.inn is not None,
        bool(company.contacts.get("phone")),
        bool(company.contacts.get("email")),
        bool(company.contacts.get("address")),
        bool(company.contacts.get("contact_person")),
        bool(company.contacts.get("website")),
        company.employees_count is not None,
        company.years_in_business is not None,
        bool(company.revenue_last_2y),
        company.status != "неизвестно",
    ]
    assert len(filled) == _CONFIDENCE_FIELDS
    return sum(filled) / _CONFIDENCE_FIELDS


def combine_score(relevance: float, trust: float, confidence: float, weights: dict) -> float:
    """relevance^a × trust^b × (base + (1-base)×confidence) — вынесено
    отдельно от compute_score, чтобы pipeline.py мог пересчитать total
    после Слоя 2/3 (уточнённая relevance), не пересчитывая trust/confidence
    заново и не дублируя формулу."""
    relevance_exp = weights.get("relevance_exponent", _DEFAULT_SCORE_WEIGHTS["relevance_exponent"])
    trust_exp = weights.get("trust_exponent", _DEFAULT_SCORE_WEIGHTS["trust_exponent"])
    confidence_base = weights.get("confidence_base", _DEFAULT_SCORE_WEIGHTS["confidence_base"])
    return (
        (relevance**relevance_exp)
        * (trust**trust_exp)
        * (confidence_base + (1 - confidence_base) * confidence)
    )


def weights_for_category(category: str | None, weights_cfg: dict) -> dict:
    return weights_cfg.get(category or "default", weights_cfg["default"])


_WEBSITE_DOMAIN_RE = re.compile(r"https?://(?:www\.)?([\w.\-]+)", re.IGNORECASE)


def company_domain(company: Company) -> str | None:
    """Домен сайта компании (без схемы/www), или None, если сайта нет/не
    распознан. Общая часть is_marketplace_domain ниже и
    pipeline._write_back_trusted_suppliers: заголовок страницы из выдачи
    нестабилен между прогонами (один и тот же сайт сегодня приходит как
    "Генератор бензиновый Huter DY3000L", завтра как "Купить генераторы
    Huter — официальный сайт"), а домен стабилен и есть всегда, раз
    результат вообще существует — поэтому именно он, а не имя компании,
    служит ключом поставщика в базе доверенных поставщиков
    (trusted_suppliers.py)."""
    website_entries = company.contacts.get("website", [])
    if not website_entries:
        return None
    match = _WEBSITE_DOMAIN_RE.search(website_entries[0].value)
    if match is None:
        return None
    return match.group(1).lower()


def is_marketplace_domain(company: Company, marketplace_domains: list[str]) -> bool:
    """Сайт компании — крупный B2C-маркетплейс/классифайд (см.
    config/marketplace_domains.yaml), а не собственный сайт поставщика.

    Не используется как штраф внутри score (compute_score) — это
    отдельный сигнал для ранжирования финальной выдачи (pipeline.py):
    такие компании не исключаются, а понижаются в приоритет, всплывая в
    топе только когда с других источников не набралось достаточно
    кандидатов (design-обсуждение: DNS/Ozon/Wildberries для B2B-закупки —
    не поставщик, а розница/перекупщик, но лучше показать их, чем ничего)."""
    domain = company_domain(company)
    if domain is None:
        return False
    return any(domain == d or domain.endswith("." + d) for d in marketplace_domains)


def has_attribute_mismatch(
    company: Company, query_attributes: list[ExtractedAttribute], units: dict | None = None
) -> bool:
    """Слой 0.5 (design-обсуждение — довязка attribute_extractor.py,
    которая раньше нигде не вызывалась): числовая характеристика из
    запроса ("10000 л/час") явно противоречит тому, что упомянуто в
    сниппете кандидата ("18 л/ч"), даже если по токенам название товара
    совпадает полностью.

    Атрибуты сайта извлекаются той же extract_attributes, что и у
    запроса, но БЕЗ LLM-fallback (use_llm_fallback не передаётся) —
    зовётся на каждого кандидата, а не один раз на запрос, поэтому должна
    быть дешёвой; "голые" числа на сайте без единицы рядом просто
    игнорируются, а не гадаются моделью.

    Не исключает и не заменяет relevance_llm.check_attribute_match (Слой
    3, тоже LLM, но по полному тексту сайта, не по сниппету) — тот точнее,
    но платный и только для top-N при --deep-relevance. Эта функция —
    бесплатный грубый фильтр для ВСЕХ кандидатов на Слое 1: сравнение
    исключительно по совпадающей единице измерения, поэтому осторожная
    (см. _ATTRIBUTE_MISMATCH_RATIO) — ложное совпадение случайного числа
    на сайте с той же единицей возможно, штрафуем relevance, а не
    выкидываем компанию целиком.

    Отсутствие атрибута на сайте (extract_attributes ничего не нашла) —
    НЕ несоответствие, тот же принцип "нет данных — не штраф", что и
    везде в scoring.py."""
    if not query_attributes:
        return False
    text_parts = [company.name.value]
    for candidate in company.raw_candidates:
        if candidate.description_raw:
            text_parts.append(candidate.description_raw)
    site_attributes = extract_attributes(" ".join(text_parts), units=units).attributes

    for q_attr in query_attributes:
        try:
            q_value = float(q_attr.value)
        except ValueError:
            continue
        if q_value <= 0:
            continue
        for s_attr in site_attributes:
            if s_attr.unit != q_attr.unit:
                continue
            try:
                s_value = float(s_attr.value)
            except ValueError:
                continue
            if s_value <= 0:
                continue
            ratio = s_value / q_value
            if ratio < 1 / _ATTRIBUTE_MISMATCH_RATIO or ratio > _ATTRIBUTE_MISMATCH_RATIO:
                return True
    return False


def compute_score(
    company: Company,
    normalized_query: NormalizedQuery,
    weights: dict | None = None,
    clean_query_text: str | None = None,
) -> ScoreBreakdown:
    """`clean_query_text` — см. compute_relevance/query_tokens."""
    weights_cfg = weights if weights is not None else load_scoring_weights()
    w = weights_for_category(normalized_query.category, weights_cfg)

    relevance = compute_relevance(company, normalized_query, clean_query_text=clean_query_text)
    trust = compute_trust(company, w)
    confidence = compute_confidence(company)
    total = combine_score(relevance, trust, confidence, w)

    return ScoreBreakdown(relevance=relevance, trust=trust, confidence=confidence, total=total)
