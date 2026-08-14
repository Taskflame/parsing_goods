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

from procurement_search.config import load_scoring_weights
from procurement_search.models import Company, FieldValue, ScoreBreakdown, VerificationFlag
from procurement_search.query_normalizer import NormalizedQuery

# Порядок альтернатив важен: дробное число (напр. "2,5") должно матчиться
# раньше отдельных цифр, иначе "2,5 квт" разваливается на токены "2" и "5" —
# два несвязанных однозначных числа, которые совпадают почти с чем угодно
# (телефоны, годы, другие мощности) и на практике убивают сигнал атрибута.
_TOKEN_RE = re.compile(r"[а-яa-zё]+|\d+[.,]\d+|\d+", re.IGNORECASE)

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
    return {t.lower().replace(",", ".") for t in _TOKEN_RE.findall(text)}


def query_tokens(normalized_query: NormalizedQuery) -> set[str]:
    """Множество токенов запроса — общее для Слоя 1 (здесь) и Слоя 2
    (site_relevance.py), чтобы не дублировать токенизацию в двух местах."""
    tokens: set[str] = set()
    for term in normalized_query.search_terms:
        tokens |= _tokenize(term)
    return tokens


def compute_relevance(company: Company, normalized_query: NormalizedQuery) -> float:
    """Слой 1: пересечение токенов запроса с текстом сниппетов кандидатов.
    Сниппет — то, что источник (Yandex/DDG/каталог) сам показал в выдаче,
    не текст с сайта компании — грубее Слоя 2, но бесплатно и мгновенно."""
    q_tokens = query_tokens(normalized_query)
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


def compute_score(
    company: Company, normalized_query: NormalizedQuery, weights: dict | None = None
) -> ScoreBreakdown:
    weights_cfg = weights if weights is not None else load_scoring_weights()
    w = weights_for_category(normalized_query.category, weights_cfg)

    relevance = compute_relevance(company, normalized_query)
    trust = compute_trust(company, w)
    confidence = compute_confidence(company)
    total = combine_score(relevance, trust, confidence, w)

    return ScoreBreakdown(relevance=relevance, trust=trust, confidence=confidence, total=total)
