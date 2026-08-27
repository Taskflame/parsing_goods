"""Модель данных пайплайна.

Разделение Candidate / Company намеренное (см. docs/design_doc.md, §1):
Candidate — сырой, ещё не подтверждённый результат генерации кандидатов;
Company — резолвленная в ИНН запись с фактами по полям и историей источников.
Кандидат, не прошедший резолвинг, остаётся Candidate и не должен попадать
в финальную выдачу как "поставщик" (только как "неподтверждённый лид").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


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
    OUT_OF_STOCK = "нет в наличии"
    NOT_CHECKED = "не проверено"


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
