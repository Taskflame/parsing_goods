"""Запасной вариант к yandexgpt_classifier.py (боевой провайдер по
умолчанию) — облачный провайдер на случай отката, если YandexGPT
недоступен или не устраивает по качеству (LLM_PROVIDER=cloudru).

Cloud.ru Evolution Foundation Models — OpenAI-совместимый API поверх 20+
моделей (GLM, Qwen, DeepSeek, MiniMax, GigaChat), выбран из трёх
рассмотренных вариантов (design-обсуждение: Yandex AI Studio — свой
отдельный эндпоинт не для чата, GigaChat — несовместимый протокол, нужен
адаптер) именно за нативную OpenAI-совместимость — минимум своего кода.

Авторизация — статический API-ключ (Bearer), как у Anthropic/OpenAI, а НЕ
пара key_id/secret с обменом на IAM-токен, как сперва предполагалось по
пакету `evolution-openai` и его README (github.com/cloud-ru/evolution-openai-python):
тот путь на практике дал 401 на реальном ключе. Быстрый старт Cloud.ru
(cloud.ru/docs/foundation-models/ug/topics/quickstart) показывает более
простой вариант — обычный `openai` SDK, `api_key=<Key Secret из
API-ключа>`, `base_url=".../v1"` — который и используется здесь.
Никакой дополнительной зависимости сверх уже установленного `openai`
пакета не требуется (`evolution-openai` больше не используется).

Structured output — `response_format={"type": "json_schema", ...}`, а не
более мягкий `json_object`, который стоял здесь изначально из
соображений переносимости между 20+ моделями каталога: проверено вживую
на реальном ключе (design-обсуждение) — `anthropic/claude-haiku-4.5`
через шлюз Cloud.ru отклоняет `json_object` ошибкой 400 ("Input should
be 'json_schema'"), требует именно строгий режим. Ответ дополнительно
валидируется через Pydantic (`Model.model_validate_json`) — если модель
вернёт JSON с недостающими/лишними полями, упадёт с понятной ошибкой
валидации, а не тихо отдаст мусор.
"""

from __future__ import annotations

import os

from pydantic import BaseModel

from procurement_search.llm_schemas import (
    AttributeBatchGuess,
    AttributeMatchVerdict,
    BrandGuess,
    ContactGuess,
    ListingTypeVerdict,
    RelevanceVerdict,
    StockVerdict,
)

CLOUDRU_BASE_URL = os.environ.get("CLOUDRU_BASE_URL", "https://foundation-models.api.cloud.ru/v1")
# Каталог из 20+ моделей (github.com/cloud-ru/evolution-openai-python,
# cloud.ru/docs/foundation-models) — конкретный ID модели специфичен для
# аккаунта/тарифа и не зашит сюда как дефолт, чтобы не выдавать
# непроверенную догадку за рабочее значение; обязателен явный CLOUDRU_MODEL.
CLOUDRU_MODEL = os.environ.get("CLOUDRU_MODEL")


def _client():
    """Ленивый импорт + создание клиента — тот же приём, что у
    yandexgpt_classifier._client(): явный импорт делается только тем
    кодом, который реально включил LLM_PROVIDER=cloudru, хотя пакет
    `openai` и так в requirements.txt (после ухода от evolution-openai
    остаётся как прямая, а не транзитивная зависимость)."""
    from openai import OpenAI

    api_key = os.environ.get("CLOUDRU_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LLM_PROVIDER=cloudru требует CLOUDRU_API_KEY (Key Secret из "
            "API-ключа, выпущенного в карточке модели в консоли Cloud.ru — "
            "Evolution → Foundation Models → модель → 'Использовать')"
        )
    return OpenAI(api_key=api_key, base_url=CLOUDRU_BASE_URL)


def _ask_json(
    system_prompt: str,
    user_content: str,
    output_model: type[BaseModel],
    client=None,
    model: str | None = None,
) -> BaseModel:
    """Общая часть всех classify_*_with_cloudru — просит структурированный
    вывод (response_format={"type": "json_schema", ...}), результат
    валидируем через Pydantic.

    Изначально здесь стоял более мягкий {"type": "json_object"} — расчёт
    был на переносимость между разными моделями каталога (20+ моделей, не
    все от одного вендора). Проверено вживую на реальном ключе
    (design-обсуждение): `anthropic/claude-haiku-4.5` через шлюз Cloud.ru
    отклоняет "json_object" ошибкой 400 ("Input should be 'json_schema'")
    — этому конкретному эндпоинту нужен строгий json_schema, и мягкий
    режим оказался не переносимее, а просто нерабочим для этой модели.

    `model`, как и YANDEX_FM_MODEL у yandexgpt_classifier.py — параметр,
    а не только глобальная переменная модуля: CLOUDRU_MODEL читается один
    раз при импорте
    (os.environ.get на уровне модуля), поэтому тестам и вызывающему коду,
    которым нужно переопределить модель после импорта, важно иметь
    возможность передать её явно."""
    model = model or CLOUDRU_MODEL
    if model is None:
        raise RuntimeError(
            "LLM_PROVIDER=cloudru требует CLOUDRU_MODEL — ID модели из каталога "
            "Cloud.ru Foundation Models (см. cloud.ru/docs/foundation-models)"
        )
    client = client or _client()
    # strict-режим OpenAI-совместимых structured outputs (подтверждено
    # вживую на реальном ключе, design-обсуждение): "strict" — обязательное
    # поле в json_schema, а сама схема обязана явно запрещать лишние поля
    # через additionalProperties=False — Pydantic его по умолчанию не
    # проставляет, но без него strict-валидация Cloud.ru отклоняет запрос.
    schema_hint = output_model.model_json_schema()
    schema_hint["additionalProperties"] = False
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": output_model.__name__, "schema": schema_hint, "strict": True},
        },
    )
    raw_json = response.choices[0].message.content
    return output_model.model_validate_json(raw_json)


def classify_relevance_with_cloudru(
    raw_query: str, site_text: str, client=None, model: str | None = None
) -> RelevanceVerdict:
    """Тот же контракт, что у yandexgpt_classifier.classify_relevance_with_yandexgpt (llm_schemas.RelevanceVerdict)."""
    return _ask_json(
        system_prompt=(
            "Ты проверяешь, продаёт ли компания на своём сайте конкретный товар/услугу, "
            "указанный байером — именно как позицию в номенклатуре, а не случайное "
            "упоминание в новости или статье."
        ),
        user_content=(
            f"Запрос байера: {raw_query!r}\n\n"
            f"Текст сайта компании (может быть обрезан): {site_text[:8000]!r}"
        ),
        output_model=RelevanceVerdict,
        client=client,
        model=model,
    )


def classify_attribute_match_with_cloudru(
    raw_query: str, site_text: str, client=None, model: str | None = None
) -> AttributeMatchVerdict:
    """Тот же контракт, что у yandexgpt_classifier.classify_attribute_match_with_yandexgpt (llm_schemas.AttributeMatchVerdict)."""
    return _ask_json(
        system_prompt=(
            "Ты сверяешь конкретный товар/услугу на сайте компании с характеристиками, "
            "которые указал байер в запросе — числовыми (мощность, объём, производительность, "
            "размер и т.п.) и качественными (тип, материал, назначение, бренд). Товар того же "
            "класса, но с явно другим значением параметра (например, запрошено 10000 л/час, "
            "а на сайте — 18 л/ч) — это НЕСООТВЕТСТВИЕ, даже если название товара совпадает. "
            "Если характеристика из запроса просто не упомянута в тексте сайта — это НЕ "
            "несоответствие; засчитывай mismatch только при прямом противоречии."
        ),
        user_content=(
            f"Запрос байера: {raw_query!r}\n\n"
            f"Текст сайта компании (может быть обрезан): {site_text[:8000]!r}"
        ),
        output_model=AttributeMatchVerdict,
        client=client,
        model=model,
    )


def classify_stock_status_with_cloudru(
    site_text: str, client=None, model: str | None = None
) -> StockVerdict:
    """Тот же контракт, что у yandexgpt_classifier.classify_stock_status_with_yandexgpt (llm_schemas.StockVerdict)."""
    return _ask_json(
        system_prompt=(
            "Ты ищешь на тексте сайта компании явный, недвусмысленный маркер того, что "
            "товар/позиция отсутствует: 'нет в наличии', 'товар закончился', 'распродано', "
            "'снят с производства', 'временно недоступен' и подобные плашки. out_of_stock=true "
            "только при прямом текстовом указании на отсутствие, иначе out_of_stock=false."
        ),
        user_content=f"Текст сайта компании (может быть обрезан): {site_text[:8000]!r}",
        output_model=StockVerdict,
        client=client,
        model=model,
    )


def classify_listing_type_with_cloudru(
    raw_query: str, title: str, snippet: str | None, client=None, model: str | None = None
) -> ListingTypeVerdict:
    """Тот же контракт, что у yandexgpt_classifier.classify_listing_type_with_yandexgpt (llm_schemas.ListingTypeVerdict)."""
    return _ask_json(
        system_prompt=(
            "Ты определяешь тип страницы по заголовку и сниппету из поисковой выдачи: "
            "это карточка товара / страница каталога / прайс-лист, где товар можно "
            "купить, заказать или узнать цену у компании — или это статья, обзор, "
            "видео, новость, форумное обсуждение, инструкция по эксплуатации. "
            "is_listing=true только для первого варианта."
        ),
        user_content=f"Запрос байера: {raw_query!r}\nЗаголовок результата: {title!r}\nСниппет: {snippet!r}",
        output_model=ListingTypeVerdict,
        client=client,
        model=model,
    )


def classify_attributes_batch_with_cloudru(
    raw_query: str,
    naked_numbers: list[str],
    units: dict[str, list[str]],
    client=None,
    model: str | None = None,
) -> AttributeBatchGuess:
    """Тот же контракт, что у yandexgpt_classifier.classify_attributes_batch_with_yandexgpt
    (llm_schemas.AttributeBatchGuess) — один batch-вызов на все "голые" числа
    запроса разом, с реальными алиасами units.yaml в промпте для явного
    fuzzy-матчинга опечаток/сокращений/склонений."""
    units_hint = "\n".join(f"{code}: {', '.join(aliases)}" for code, aliases in units.items())
    return _ask_json(
        system_prompt=(
            "Ты помогаешь нормализовать числовые характеристики в запросе байера "
            "B2B-закупки. Тебе передан список чисел из запроса, для которых рядом НЕ "
            "нашлось точного совпадения по словарю обычным сопоставлением. Для КАЖДОГО "
            "числа реши, относится ли оно к единице измерения из словаря ниже, и к "
            "какой именно — сопоставляй написание единицы рядом с числом в запросе с "
            "каноническим кодом словаря, даже если написание отличается опечаткой, "
            "сокращением, транслитерацией или нестандартным склонением (например, "
            "\"киловат\", \"киловатт\", \"кВт\", \"kW\" — всё это код \"квт\"). Если число — "
            "это количество штук без явной единицы, часть артикула/названия, год или "
            "что-то ещё, не являющееся характеристикой с единицей измерения из "
            "словаря — верни unit: null для него, но всё равно включи его в ответ. "
            "Выбирай unit СТРОГО из кодов словаря, без выдумывания новых. Запятую в "
            "value замени на точку. `raw` — точная подстрока из исходного запроса "
            "(число вместе со словом единицы рядом, если оно есть), без изменений — "
            "нужна дословно для вырезания из текста.\n\n"
            f"Канонический словарь единиц (код: примеры написаний):\n{units_hint}"
        ),
        user_content=(
            f"Запрос байера: {raw_query!r}\n"
            f"Числа, для которых нужно определить единицу: {naked_numbers}"
        ),
        output_model=AttributeBatchGuess,
        client=client,
        model=model,
    )


def extract_brand_with_cloudru(raw_query: str, client=None, model: str | None = None) -> BrandGuess:
    """Тот же контракт, что у yandexgpt_classifier.extract_brand_with_yandexgpt
    (llm_schemas.BrandGuess)."""
    return _ask_json(
        system_prompt=(
            "Ты определяешь, упомянут ли в запросе байера конкретный бренд/производитель "
            "товара — собственное имя компании или торговая марка (например, «Пульсар», "
            "«Bosch», «FinePower»), а не тип товара, материал или характеристика. Если "
            "бренд явно упомянут — верни его ТОЧНО как написано в запросе (сохраняя "
            "регистр). Если бренда в запросе нет — верни brand: null. Не путай бренд с "
            "моделью/артикулом — буквенно-цифровые коды вроде «DA0055G3» это НЕ бренд, "
            "это код конкретного товара у конкретного (обычно другого) производителя."
        ),
        user_content=f"Запрос байера: {raw_query!r}",
        output_model=BrandGuess,
        client=client,
        model=model,
    )


def extract_contacts_with_cloudru(site_text: str, client=None, model: str | None = None) -> ContactGuess:
    """Тот же контракт, что у yandexgpt_classifier.extract_contacts_with_yandexgpt
    (llm_schemas.ContactGuess)."""
    return _ask_json(
        system_prompt=(
            "Ты ищешь на тексте сайта компании контактную информацию: номер телефона, "
            "email, физический адрес. Верни то, что реально нашёл, ДОСЛОВНО как написано "
            "в тексте — не нормализуй и не переформатируй (например, +7(495)... оставь "
            "ровно как в тексте, не переписывай в другой вид) и не придумывай ничего, "
            "чего в тексте нет. Если какого-то из полей в тексте нет — верни null для "
            "него, а не догадку."
        ),
        user_content=f"Текст сайта компании (может быть обрезан): {site_text[:8000]!r}",
        output_model=ContactGuess,
        client=client,
        model=model,
    )
