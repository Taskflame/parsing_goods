"""Шаг [5] пайплайна: дедупликация и слияние кандидатов из разных источников.

До интеграции с ЕГРЮЛ (см. enrichment.py) у нас нет ИНН как первичного
ключа, поэтому дедуп идёт по суррогатным ключам — телефону и домену сайта,
с fallback на нечёткое сравнение названия. Телефон/домен выбраны первыми,
потому что они куда устойчивее к вариациям написания названия компании
("ООО Ромашка" / "Ромашка" / "Ромашка ООО"), чем сам текст названия.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from procurement_search.models import Candidate

_LEGAL_FORMS = [
    "ооо", "зао", "оао", "пао", "ао", "ип", "чп", "нко", "тоо",
    "общество с ограниченной ответственностью",
    "закрытое акционерное общество",
    "открытое акционерное общество",
    "публичное акционерное общество",
    "индивидуальный предприниматель",
]
_LEGAL_FORM_RE = re.compile(
    r"\b(" + "|".join(re.escape(f) for f in _LEGAL_FORMS) + r')\b\.?', re.IGNORECASE
)
_PUNCT_RE = re.compile(r'["\'«»,.\-]')
_DOMAIN_RE = re.compile(r"@([\w.\-]+)")


def normalize_name(name: str) -> str:
    name = _LEGAL_FORM_RE.sub(" ", name)
    name = _PUNCT_RE.sub(" ", name)
    return " ".join(name.lower().split())


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    # приводим к последним 10 цифрам (национальный номер РФ без кода страны),
    # чтобы "+7 900..." и "8 900..." матчились как один номер
    return digits[-10:] if len(digits) >= 10 else digits


def extract_domain(email: str | None, website: str | None) -> str | None:
    """Домен для дедупа. `website` должен быть собственным сайтом компании,
    а НЕ URL страницы листинга на pulscen.ru/optlist.ru — иначе все
    кандидаты с одного источника получат одинаковый домен и ложно
    схлопнутся в одну компанию (см. Candidate.website)."""
    if email:
        m = _DOMAIN_RE.search(email)
        if m:
            return m.group(1).lower()
    if website:
        m = re.search(r"https?://(?:www\.)?([\w.\-]+)", website)
        if m:
            return m.group(1).lower()
    return None


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def dedup_candidates(
    candidates: list[Candidate], name_similarity_threshold: float = 0.85
) -> list[list[Candidate]]:
    """Группирует кандидатов, относящихся к одной компании.

    Возвращает список групп; порядок кандидатов внутри группы сохранён.
    Жадный алгоритм O(n^2) — приемлемо для объёма выдачи одного запроса
    (десятки-сотни кандидатов), для промышленного объёма потребуется
    индексация по ключам вместо полного перебора.
    """
    groups: list[list[Candidate]] = []

    for candidate in candidates:
        cand_phone = normalize_phone(candidate.phone_raw)
        cand_domain = extract_domain(candidate.email_raw, candidate.website)

        matched_group = None
        for group in groups:
            for existing in group:
                existing_phone = normalize_phone(existing.phone_raw)
                existing_domain = extract_domain(existing.email_raw, existing.website)

                phone_match = cand_phone and existing_phone and cand_phone == existing_phone
                domain_match = cand_domain and existing_domain and cand_domain == existing_domain
                name_match = _name_similarity(candidate.name_raw, existing.name_raw) >= (
                    name_similarity_threshold
                )

                if phone_match or domain_match or name_match:
                    matched_group = group
                    break
            if matched_group is not None:
                break

        if matched_group is not None:
            matched_group.append(candidate)
        else:
            groups.append([candidate])

    return groups
