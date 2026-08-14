"""LLM-ассистированная классификация запроса — fallback над словарным
матчингом в query_normalizer.py.

Design_doc §1: LLM используется только на краях пайплайна (нормализация
запроса, извлечение фактов из текста) и никогда не является источником
факта о поставщике. Здесь LLM решает узкую задачу: если словарное
совпадение по синонимам не сработало (например, байер написал запрос
непривычной формулировкой — "покрыть металл цинком" вместо "гальванические
покрытия"), спросить модель, к какой из УЖЕ СУЩЕСТВУЮЩИХ категорий
в config/categories.yaml это ближе всего. Модель не придумывает новую
категорию и не является источником домена/ОКВЭД — только выбирает из
готового списка, что оставляет справочник (config/categories.yaml)
единственным источником истины по кодам и реестрам.

Используется официальный Anthropic Python SDK (`pip install anthropic`),
а не самодельные HTTP-запросы. Требует переменную окружения
ANTHROPIC_API_KEY (или профиль `ant auth login`) — при её отсутствии
классификатор не инициализируется, и вызывающий код (query_normalizer.py)
должен трактовать это как "LLM недоступен", оставаясь на словарном пути.
"""

from __future__ import annotations

import os

from anthropic import Anthropic
from pydantic import BaseModel

# claude-opus-5 — универсальный выбор по умолчанию. Для этой конкретной
# задачи (короткая классификация по закрытому списку категорий) заметно
# дешевле claude-haiku-4-5 даёт сопоставимое качество — если в проде это
# станет частым вызовом (не 4 тестовых категории из ТЗ, а сотни), стоит
# сознательно переключиться и явно решить это, а не унаследовать выбор
# по умолчанию. Переопределяется через ANTHROPIC_MODEL.
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")


class CategoryMatch(BaseModel):
    category: str | None
    reasoning: str


class RelevanceVerdict(BaseModel):
    is_relevant: bool
    reasoning: str


class AttributeGuess(BaseModel):
    unit: str | None
    reasoning: str


def classify_with_llm(
    raw_query: str,
    known_categories: list[str],
    client: Anthropic | None = None,
    model: str = DEFAULT_MODEL,
) -> CategoryMatch:
    """Просит модель сопоставить запрос байера с одной из известных категорий.

    Вызывается только когда словарное совпадение в query_normalizer.py не
    нашло ни одного пересечения токенов — детерминированный путь остаётся
    приоритетным (см. query_normalizer.normalize_query, use_llm_fallback).
    """
    client = client or Anthropic()
    response = client.messages.parse(
        model=model,
        max_tokens=1024,
        system=(
            "Ты помогаешь классифицировать запрос байера по категориям "
            "закупки. Выбери категорию СТРОГО из переданного списка, без "
            "выдумывания новых. Если ни одна категория семантически не "
            "подходит — верни category: null."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Запрос байера: {raw_query!r}\n"
                    f"Известные категории: {known_categories}"
                ),
            }
        ],
        output_format=CategoryMatch,
    )
    return response.parsed_output


def classify_relevance_with_llm(
    raw_query: str,
    site_text: str,
    client: Anthropic | None = None,
    model: str = DEFAULT_MODEL,
) -> RelevanceVerdict:
    """Слой 3 уточнения релевантности (см. relevance_llm.py, site_relevance.py):
    проверяет, продаётся ли товар из запроса байера на сайте кандидата НА
    САМОМ ДЕЛЕ — как позиция в номенклатуре, а не случайное упоминание в
    новости/статье. Слой 2 (пересечение токенов по тексту сайта) этого не
    различает; для этого и нужна точечная LLM-проверка поверх него.

    Вызывается только для уже отфильтрованных top-N кандидатов (не для
    всей выдачи) — см. pipeline.py.
    """
    client = client or Anthropic()
    response = client.messages.parse(
        model=model,
        max_tokens=512,
        system=(
            "Ты проверяешь, продаёт ли компания на своём сайте конкретный товар/услугу, "
            "указанный байером — именно как позицию в номенклатуре, а не случайное "
            "упоминание в новости, статье или общем описании деятельности. Текст сайта "
            "может быть неполным или содержать мусор вёрстки — работай с тем, что есть."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Запрос байера: {raw_query!r}\n\n"
                    f"Текст сайта компании (может быть обрезан): {site_text[:8000]!r}"
                ),
            }
        ],
        output_format=RelevanceVerdict,
    )
    return response.parsed_output


def classify_attribute_with_llm(
    raw_query: str,
    number: str,
    known_units: list[str],
    client: Anthropic | None = None,
    model: str = DEFAULT_MODEL,
) -> AttributeGuess:
    """Слой 0 (см. attribute_extractor.py): для числа, рядом с которым
    словарный regex не нашёл известной единицы измерения, просит модель
    подобрать единицу СТРОГО из уже существующего словаря
    (config/units.yaml) — по контексту всего запроса, а не только цифры.

    Вызывается только когда в запросе остались "голые" числа после
    словарного прохода — см. attribute_extractor.extract_attributes,
    use_llm_fallback.
    """
    client = client or Anthropic()
    response = client.messages.parse(
        model=model,
        max_tokens=512,
        system=(
            "Ты помогаешь понять, является ли число в запросе байера технической "
            "характеристикой товара (например, мощность, размер, масса), и если да — "
            "к какой единице измерения оно относится. Выбери единицу СТРОГО из "
            "переданного списка кодов, без выдумывания новых. Если число — это "
            "количество штук без явной единицы, часть артикула/названия, год или "
            "что-то ещё, не являющееся характеристикой с единицей измерения из "
            "списка — верни unit: null."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Запрос байера: {raw_query!r}\n"
                    f"Число, для которого нужно определить единицу: {number!r}\n"
                    f"Известные единицы измерения (коды): {known_units}"
                ),
            }
        ],
        output_format=AttributeGuess,
    )
    return response.parsed_output
