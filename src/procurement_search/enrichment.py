"""Шаги [3]-[4] пайплайна: резолвинг в ИНН и обогащение фактами.

Это самая важная часть системы для реального использования (design_doc,
§11) — без резолвинга в ЕГРЮЛ нет отсева ликвидированных компаний, нет
истории переименований, нет проверки на банкротство/РНП. `NullEnricher` —
честная заглушка (ничего не резолвит, помечает всё как непроверенное).
`DadataEnricher` — рабочая интеграция с Dadata suggest API (design_doc
§5.5, "быстрый старт"): резолвит компанию по названию в ИНН/ОГРН и статус
из ЕГРЮЛ, что напрямую закрывает ТЗ п.4 "Отсев неактуальных данных".

Оба реализуют один интерфейс `Enricher`, так что pipeline.py не меняется
при переключении между ними — конструктор пайплайна просто получает
другой объект enricher.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import date

import requests

from procurement_search.models import Candidate, Company, FieldValue, VerificationFlag

logger = logging.getLogger(__name__)

DADATA_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

# Dadata возвращает статус ЕГРЮЛ одним из этих значений (data.state.status).
# Отображение в наши статусы — то, что pipeline.DEAD_COMPANY_STATUSES потом
# использует для жёсткого исключения ликвидированных из выдачи (design_doc
# §1, ТЗ п.4 "исключать прекративших существование").
_STATUS_MAP = {
    "ACTIVE": "действующая",
    "LIQUIDATED": "ликвидирована",
    "LIQUIDATING": "в процессе ликвидации",
    "REORGANIZING": "в процессе реорганизации",
}

_TOKEN_RE = re.compile(r"[а-яa-zё0-9]+", re.IGNORECASE)


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _contacts_from_candidates(candidate_group: list[Candidate]) -> dict[str, list[FieldValue]]:
    """Общая часть NullEnricher и DadataEnricher: контакты со скрапинга
    всегда попадают в карточку как UNVERIFIED — ни один из энричеров не
    проверяет актуальность телефона/email (это отдельная задача, см.
    verify_contacts.py), поэтому оба честно оставляют эти поля непроверенными."""
    contacts: dict[str, list[FieldValue]] = {"phone": [], "email": [], "address": []}
    for c in candidate_group:
        if c.phone_raw:
            contacts["phone"].append(
                FieldValue(c.phone_raw, c.source, c.scraped_at, VerificationFlag.UNVERIFIED)
            )
        if c.email_raw:
            contacts["email"].append(
                FieldValue(c.email_raw, c.source, c.scraped_at, VerificationFlag.UNVERIFIED)
            )
        if c.address_raw:
            contacts["address"].append(
                FieldValue(c.address_raw, c.source, c.scraped_at, VerificationFlag.UNVERIFIED)
            )
    return contacts


class Enricher(ABC):
    @abstractmethod
    def build_company(self, candidate_group: list[Candidate]) -> Company:
        """Строит Company из группы кандидатов, относящихся к одной организации
        (группа — результат dedup.dedup_candidates)."""
        ...

    def re_resolve(self, company: Company, legal_name: str, candidate_group: list[Candidate]) -> None:
        """Повторная попытка резолвинга в ЕГРЮЛ с более точным названием
        компании, найденным на Слое 2 (см. pipeline._attach_legal_name) —
        вызывается, только когда build_company не смог резолвиться по
        исходному имени кандидата (заголовок товарной карточки, не
        название юрлица). Мутирует `company` на месте (inn/ogrn/name/
        status/contacts), возврата нет.

        По умолчанию — no-op: NullEnricher никуда не резолвит, у него
        нечего повторять. Единственное содержательное переопределение —
        DadataEnricher.re_resolve ниже."""
        return


class NullEnricher(Enricher):
    """Заглушка: не резолвит ИНН, не ходит ни в один внешний реестр.

    Собирает Company напрямую из кандидатов с полями, помеченными
    UNVERIFIED — честно отражает то, что реальной проверки не производилось,
    вместо того чтобы притворяться, что данные подтверждены.
    """

    def build_company(self, candidate_group: list[Candidate]) -> Company:
        primary = candidate_group[0]
        return Company(
            inn=None,
            ogrn=None,
            name=FieldValue(
                primary.name_raw, primary.source, primary.scraped_at, VerificationFlag.UNVERIFIED
            ),
            status="неизвестно",
            contacts=_contacts_from_candidates(candidate_group),
            sources=sorted({c.source for c in candidate_group}),
            raw_candidates=candidate_group,
        )


class DadataEnricher(Enricher):
    """Резолвинг в ЕГРЮЛ через Dadata suggest API — закрывает ТЗ п.4
    "Отсев неактуальных данных":

      - статус компании (действующая/ликвидирована/...) из ЕГРЮЛ кладётся
        в Company.status; pipeline.DEAD_COMPANY_STATUSES потом жёстко
        исключает ликвидированные из финальной выдачи;
      - официальное название берётся из ЕГРЮЛ, а не из карточки на
        pulscen/optlist — если компания сменила название или
        реорганизовалась, в отчёте будет актуальное имя, а не то, что
        осталось в старом объявлении на сайте-каталоге;
      - юридический адрес из ЕГРЮЛ добавляется первым в список с флагом
        "подтверждён" (не заменяет адрес со скрапинга, а дополняет его).

    Не решает целиком: контроль актуальности ТЕЛЕФОНА/EMAIL (нужна
    отдельная проверка — см. verify_contacts.py) и полную историю всех
    прошлых наименований при реорганизации (Dadata suggest даёт только
    текущее состояние ЕГРЮЛ; полная история переименований — платная
    выписка, вне бесплатного тарифа Dadata).

    Матчинг по имени неоднозначен для общих названий ("Ромашка", "Альфа") —
    запрашиваем top-5 у Dadata и выбираем тот вариант, чей юридический адрес
    сильнее всего пересекается по словам с адресом, который дали кандидаты
    (если адреса нет ни у кого — берём top-1 по ранжированию Dadata). Это
    эвристика, а не гарантия точного матчинга; для более надёжного
    резолвинга в проде нужен ввод региона/ИНН от пользователя, где он
    известен.
    """

    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ):
        if not api_key:
            raise ValueError("DadataEnricher требует api_key — см. docs/design_doc.md §5.5")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout

    def build_company(self, candidate_group: list[Candidate]) -> Company:
        primary = candidate_group[0]
        contacts = _contacts_from_candidates(candidate_group)
        sources = sorted({c.source for c in candidate_group})

        match = self._suggest(primary.name_raw, candidate_group)
        if match is None:
            # Не резолвится в ЕГРЮЛ -> неподтверждённый лид, не поставщик
            # (design_doc §1) — статус "неизвестно", а не выдуманный.
            return Company(
                inn=None,
                ogrn=None,
                name=FieldValue(
                    primary.name_raw, primary.source, primary.scraped_at, VerificationFlag.UNVERIFIED
                ),
                status="неизвестно",
                contacts=contacts,
                sources=sources,
                raw_candidates=candidate_group,
            )

        inn, ogrn, name_field, status, address_field = _fields_from_suggestion(
            match, fallback_name=primary.name_raw, source_label="ЕГРЮЛ (Dadata)"
        )
        if address_field is not None:
            contacts.setdefault("address", []).insert(0, address_field)

        return Company(
            inn=inn,
            ogrn=ogrn,
            name=name_field,
            status=status,
            contacts=contacts,
            sources=sources,
            raw_candidates=candidate_group,
        )

    def re_resolve(self, company: Company, legal_name: str, candidate_group: list[Candidate]) -> None:
        """См. Enricher.re_resolve — вызывается pipeline._attach_legal_name
        только когда build_company не резолвился с первой попытки
        (company.inn is None), с названием, найденным на Слое 2, вместо
        заголовка товарной карточки. Не тратит запрос, если компания уже
        резолвлена — не перепроверяем то, что уже подтвердилось."""
        if company.inn is not None:
            return
        match = self._suggest(legal_name, candidate_group)
        if match is None:
            return

        inn, ogrn, name_field, status, address_field = _fields_from_suggestion(
            match, fallback_name=legal_name, source_label="ЕГРЮЛ (Dadata, по названию со Слоя 2)"
        )
        if address_field is not None:
            company.contacts.setdefault("address", []).insert(0, address_field)

        company.inn = inn
        company.ogrn = ogrn
        company.name = name_field
        company.status = status

    def _suggest(self, name: str, candidate_group: list[Candidate]) -> dict | None:
        try:
            resp = self.session.post(
                DADATA_SUGGEST_URL,
                json={"query": name, "count": 5},
                headers={
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException:
            logger.warning(
                "Dadata suggest не сработал для %r — компания остаётся "
                "неподтверждённым лидом (проверьте DADATA_API_KEY и лимиты)",
                name,
                exc_info=True,
            )
            return None

        suggestions = resp.json().get("suggestions") or []
        return _pick_best_suggestion(suggestions, candidate_group)


def _fields_from_suggestion(
    match: dict, fallback_name: str, source_label: str
) -> tuple[str | None, str | None, FieldValue, str, FieldValue | None]:
    """Общая часть DadataEnricher.build_company и .re_resolve — превращает
    один suggestion Dadata в (inn, ogrn, name FieldValue, status,
    address FieldValue|None). Вынесено отдельно, чтобы re_resolve не
    дублировал разбор ответа build_company построчно (см. design-
    обсуждение про добавление re_resolve для повторной попытки с
    названием, найденным на Слое 2, а не заголовком товарной карточки)."""
    data = match.get("data") or {}
    name_block = data.get("name") or {}
    official_name = (
        name_block.get("full_with_opf")
        or name_block.get("short_with_opf")
        or match.get("value")
        or fallback_name
    )
    status_raw = (data.get("state") or {}).get("status") or ""
    status = _STATUS_MAP.get(status_raw, "неизвестно")

    today = date.today()
    name_field = FieldValue(official_name, source_label, today, VerificationFlag.CONFIRMED)
    address_value = (data.get("address") or {}).get("value")
    address_field = (
        FieldValue(address_value, source_label, today, VerificationFlag.CONFIRMED)
        if address_value
        else None
    )
    return data.get("inn"), data.get("ogrn"), name_field, status, address_field


def _pick_best_suggestion(suggestions: list[dict], candidate_group: list[Candidate]) -> dict | None:
    if not suggestions:
        return None

    candidate_address_tokens = _tokenize(
        " ".join(c.address_raw or "" for c in candidate_group)
    )
    if not candidate_address_tokens:
        return suggestions[0]

    best = suggestions[0]
    best_overlap = -1
    for suggestion in suggestions:
        address_value = ((suggestion.get("data") or {}).get("address") or {}).get("value") or ""
        overlap = len(candidate_address_tokens & _tokenize(address_value))
        if overlap > best_overlap:
            best_overlap = overlap
            best = suggestion
    return best