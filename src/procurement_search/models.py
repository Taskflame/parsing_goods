"""Модель данных пайплайна.

Разделение Candidate / Company намеренное (см. docs/design_doc.md, §1):
Candidate — сырой, ещё не подтверждённый результат генерации кандидатов;
Company — резолвленная в ИНН запись с фактами по полям и историей источников.
Кандидат, не прошедший резолвинг, остаётся Candidate и не должен попадать
в финальную выдачу как "поставщик" (только как "неподтверждённый лид").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from procurement_search.attribute_extractor import Quantity


class VerificationFlag(str, Enum):
    CONFIRMED = "подтверждён"
    UNVERIFIED = "не проверен"
    STALE = "протух"


class StockStatus(str, Enum):
    """Наличие товара на сайте кандидата — отдельный флаг от VerificationFlag
    (не про достоверность контакта, а про факт "товар закончился"). Заполняется
    только Слоем 3 (см. relevance_llm.classify_stock_status, pipeline._refine_relevance)
    и намеренно НЕ участвует в scoring.py: это информационная плашка для байера
    в духе "сайт жив/протух" (verify_contacts.py), а не сигнал доверия/релевантности —
    результат может устареть быстрее, чем следующий перезапуск поиска, и жёстко
    понижать/выкидывать поставщика по нему было бы неоправданно."""

    IN_STOCK = "в наличии"
    CLARIFY = "уточнить наличие"
    OUT_OF_STOCK = "нет в наличии"
    NOT_CHECKED = "не проверено"


class AvailabilityStatus(str, Enum):
    """Статус остатка товара на сайте кандидата — Availability (см. ниже),
    отдельная и более богатая проверка, чем StockStatus выше: не просто
    текстовый статус, а попытка вытащить число (сколько именно), фасовку,
    минимальную партию и срок поставки (см. availability.py). Работает
    параллельно StockStatus, не заменяет его (design-обсуждение: оставлены
    оба намеренно — разный охват и разная стоимость LLM-вызова, см.
    docs/design_doc.md)."""

    IN_STOCK_QTY = "в наличии, количество указано"
    IN_STOCK = "в наличии"
    ON_ORDER = "под заказ"
    OUT_OF_STOCK = "нет в наличии"
    UNKNOWN = "нет данных"


@dataclass
class Availability:
    """Наличие товара на сайте кандидата с числом (если опубликовано) —
    заполняется availability.extract_availability, только для top-N
    кандидатов после скоринга (см. pipeline.py, тот же паттерн, что у
    Слоя 3 relevance_llm.py). НЕ участвует в relevance/knockout — тот же
    принцип, что у StockStatus выше: остаток протухает быстрее, чем
    следующий перезапуск поиска, жёстко штрафовать/выкидывать поставщика
    по нему неоправданно (см. quantity_match.py про сортировку вместо
    отсева)."""

    status: AvailabilityStatus
    quantity: Quantity | None  # найденный остаток
    pack_size: Quantity | None  # фасовка: "упаковка 5 шт", "бочка 200 л"
    min_order: Quantity | None  # минимальная партия заказа
    lead_time_days: int | None  # срок поставки под заказ, дней
    price: str | None
    source_url: str
    checked_at: datetime
    evidence: str | None  # дословный фрагмент страницы-обоснование, как у StockStatus.quote выше


@dataclass(frozen=True)
class FieldValue:
    """Одно значение поля с происхождением — источник, дата, достоверность.

    Именно этот объект (а не голая строка) кладётся в карточку компании,
    чтобы в Excel-выгрузке у каждого поля был источник и флаг (design_doc §8).
    """

    value: str
    source: str
    retrieved_at: date
    confidence: VerificationFlag = VerificationFlag.UNVERIFIED


@dataclass
class Candidate:
    """Сырой результат шага [2] "генерация кандидатов" — ещё не резолвлен в ИНН."""

    source: str
    source_url: str
    name_raw: str
    phone_raw: str | None = None
    email_raw: str | None = None
    address_raw: str | None = None
    description_raw: str | None = None
    # Собственный сайт компании (НЕ страница листинга на source_url) — только
    # его домен пригоден для дедупа по домену. source_url для CSS-fallback
    # обычно указывает на профиль компании внутри самого каталога
    # (pulscen.ru/company/..., optlist.ru/...), и его домен одинаков у ВСЕХ
    # кандидатов с этого источника — использовать source_url для домена
    # в дедупе нельзя, иначе все компании с одной площадки схлопнутся в одну.
    website: str | None = None
    scraped_at: date = field(default_factory=date.today)

    # Заполняется на шаге [3] "резолвинг", если удалось сматчить в ЕГРЮЛ.
    inn: str | None = None


@dataclass
class ScoreBreakdown:
    """Три независимых числа, не одно — намеренно (design-обсуждение по
    скорингу, см. scoring.py): compute_score больше не складывает оси
    взвешенной суммой, а перемножает relevance/trust и домножает на
    мягкий штраф за неполноту confidence. Так нулевая релевантность не
    вытягивается за счёт того, что компания крупная и старая, а нехватка
    данных (confidence) не путается с тем, что компания плохая (trust)."""

    relevance: float
    trust: float
    confidence: float
    total: float


@dataclass
class Company:
    """Резолвленная (или объединённая из нескольких кандидатов) компания."""

    inn: str | None
    ogrn: str | None
    name: FieldValue
    status: str  # "действующая" | "ликвидирована" | "в процессе банкротства" | "неизвестно"
    contacts: dict[str, list[FieldValue]] = field(default_factory=dict)
    # contacts: {"phone": [...], "email": [...], "address": [...], "contact_person": [...]}

    employees_count: FieldValue | None = None
    years_in_business: FieldValue | None = None
    revenue_last_2y: list[FieldValue] = field(default_factory=list)

    # Цена товара с сайта кандидата (Слой 2, см. pipeline._attach_site_price) —
    # единственное значение, не список: в отличие от contacts (несколько
    # источников по одному кандидату — сниппет + сайт), цена берётся
    # только с самого сайта, второго источника для неё нет. Используется
    # в pipeline._ranking_key как отдельный KPI финальной сортировки
    # (design-обсуждение: явный запрос байера — цена дешевле выше, поверх
    # уже готового списка, а не ещё один вес внутри compute_score).
    price: FieldValue | None = None

    sources: list[str] = field(default_factory=list)
    raw_candidates: list[Candidate] = field(default_factory=list)

    score: ScoreBreakdown | None = None
    stock_status: StockStatus = StockStatus.NOT_CHECKED
    # Дословная фраза с сайта, подтверждающая stock_status (например,
    # "цена уточняется у менеджера" для CLARIFY) — см.
    # relevance_llm.classify_stock_status. None, если статус выставлен по
    # умолчанию (явного маркера на сайте не нашлось) или stock_status ещё
    # NOT_CHECKED.
    stock_status_quote: str | None = None

    # Availability (см. её докстринг) — отдельная от stock_status проверка,
    # с числом/фасовкой/сроком поставки, только для top-N после скоринга
    # (см. pipeline.py, флаг check_availability). None — фича не включена
    # или кандидат не попал в top-N.
    availability: Availability | None = None
    # Значение quantity_match.Verdict (строкой, не enum-объектом — Company
    # не должна зависеть от quantity_match.py, тот же принцип, что stock_status
    # хранит только значение enum, а не сам объект-классификатор).
    availability_verdict: str | None = None
    # Готовый текст для колонки Excel/веб-таблицы (quantity_match.compare),
    # например "Недостаточно — есть всего 10 шт из 11".
    availability_verdict_text: str | None = None
