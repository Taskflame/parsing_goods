"""Боевой LLM-провайдер по умолчанию — Yandex AI Studio (Yandex Foundation
Models). Единственная запасная альтернатива — cloudru_classifier.py
(LLM_PROVIDER=cloudru, на случай отката); Anthropic/Claude и локальная
Ollama из проекта убраны (design-обсуждение: не нужны два платных облачных
провайдера сразу, а Ollama требовала локально запущенной модели, что не
подходит для боевого сервера).

Модуль почти дословно повторяет cloudru_classifier.py — тот же контракт
(OpenAI-совместимый API, response_format json_schema через стандартный
`openai` SDK), меняются только эндпоинт (https://llm.api.cloud.yandex.net/v1)
и формат имени модели.

Авторизация — статический API-ключ Yandex AI Studio (Bearer), YANDEX_FM_API_KEY
(НЕ тот же ключ, что YANDEX_SEARCH_API_KEY у sources/yandex_search.py —
разные сервисы Yandex Cloud, в общем случае разные ключи И разные
аккаунты/каталоги). Модель у Yandex адресуется не голым ID, а URI вида
gpt://<FOLDER_ID>/<MODEL_ID>/latest — folder_id для него берётся из
YANDEX_FM_FOLDER_ID, если задан, иначе из уже существующей YANDEX_FOLDER_ID
(удобный дефолт для типового случая "один аккаунт на оба сервиса" — но
если YANDEX_SEARCH_API_KEY и YANDEX_FM_API_KEY реально из разных
аккаунтов, folder_id от чужого аккаунта AI Studio не примет, для этого и
нужен отдельный YANDEX_FM_FOLDER_ID). Явно переданный `model=` (как в
тестах) используется как есть, без сборки URI.
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

YANDEX_FM_BASE_URL = os.environ.get("YANDEX_FM_BASE_URL", "https://llm.api.cloud.yandex.net/v1")
# ID модели из каталога Yandex AI Studio (например "yandexgpt" или
# "yandexgpt-lite") — не полный URI, тот собирается ниже вместе с
# YANDEX_FOLDER_ID. Обязателен явный YANDEX_FM_MODEL, как и у cloud.ru
# (CLOUDRU_MODEL) — не зашиваем непроверенную догадку как дефолт.
YANDEX_FM_MODEL = os.environ.get("YANDEX_FM_MODEL")


def _client():
    """Ленивый импорт + создание клиента — тот же приём, что у
    cloudru_classifier._client(): явный импорт только тем кодом, который
    реально включил LLM_PROVIDER=yandexgpt, хотя пакет `openai` и так в
    requirements.txt (общая зависимость с cloud.ru)."""
    from openai import OpenAI

    api_key = os.environ.get("YANDEX_FM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LLM_PROVIDER=yandexgpt требует YANDEX_FM_API_KEY (API-ключ Yandex AI "
            "Studio — не путать с YANDEX_SEARCH_API_KEY, это другой сервис Yandex Cloud)"
        )
    return OpenAI(api_key=api_key, base_url=YANDEX_FM_BASE_URL)


def _default_model() -> str | None:
    """Собирает URI дефолтной модели (gpt://<folder>/<model>/latest) из
    YANDEX_FM_MODEL + folder_id. Возвращает None, если чего-то не хватает —
    вызывающая _ask_json тогда падает с понятной ошибкой, а не с
    невалидным URI на середине строки.

    folder_id: YANDEX_FM_FOLDER_ID, если задан явно (случай "ключ AI Studio
    из другого аккаунта, чем YANDEX_SEARCH_API_KEY"), иначе YANDEX_FOLDER_ID
    (типовой случай — один аккаунт на оба сервиса Yandex Cloud)."""
    folder_id = os.environ.get("YANDEX_FM_FOLDER_ID") or os.environ.get("YANDEX_FOLDER_ID")
    if not YANDEX_FM_MODEL or not folder_id:
        return None
    return f"gpt://{folder_id}/{YANDEX_FM_MODEL}/latest"


def _ask_json(
    system_prompt: str,
    user_content: str,
    output_model: type[BaseModel],
    client=None,
    model: str | None = None,
) -> BaseModel:
    """Общая часть всех classify_*_with_yandexgpt — см.
    cloudru_classifier._ask_json про мотивацию json_schema (не json_object)
    и additionalProperties=False; тот же strict-контракт здесь ожидается
    и от Yandex AI Studio (обе платформы — OpenAI-совместимые эндпоинты
    поверх response_format json_schema)."""
    model = model or _default_model()
    if model is None:
        raise RuntimeError(
            "LLM_PROVIDER=yandexgpt требует YANDEX_FM_MODEL (ID модели из каталога "
            "Yandex AI Studio, например 'yandexgpt' или 'yandexgpt-lite') и folder_id — "
            "YANDEX_FM_FOLDER_ID, если ключ AI Studio из другого аккаунта, чем "
            "YANDEX_SEARCH_API_KEY, иначе достаточно уже заданного YANDEX_FOLDER_ID"
        )
    client = client or _client()
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


def classify_relevance_with_yandexgpt(
    raw_query: str, site_text: str, client=None, model: str | None = None
) -> RelevanceVerdict:
    """Возвращает llm_schemas.RelevanceVerdict."""
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


def classify_attribute_match_with_yandexgpt(
    raw_query: str, site_text: str, client=None, model: str | None = None
) -> AttributeMatchVerdict:
    """Возвращает llm_schemas.AttributeMatchVerdict."""
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


def classify_stock_status_with_yandexgpt(
    site_text: str, client=None, model: str | None = None
) -> StockVerdict:
    """Возвращает llm_schemas.StockVerdict."""
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


def classify_listing_type_with_yandexgpt(
    raw_query: str, title: str, snippet: str | None, client=None, model: str | None = None
) -> ListingTypeVerdict:
    """Возвращает llm_schemas.ListingTypeVerdict."""
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


def classify_attributes_batch_with_yandexgpt(
    raw_query: str,
    naked_numbers: list[str],
    units: dict[str, list[str]],
    client=None,
    model: str | None = None,
) -> AttributeBatchGuess:
    """Слой 0 (см. attribute_extractor.py): один batch-вызов на ВСЕ "голые"
    числа запроса разом (не по одному на число) — модель видит их в общем
    контексте запроса, что и дешевле (1 вызов вместо N), и меньше риск
    несогласованных решений между числами одного запроса.

    В промпт передаются не только канонические коды единиц, а реальные
    алиасы из units.yaml — явный fuzzy-матчинг опечаток/сокращений/
    склонений/транслитерации ("киловат"/"киловатт"/"кВт"/"kW" — всё это
    код "квт"), а не расчёт на то, что модель сама разберётся по общим
    знаниям, имея на входе только голый код.

    `raw` в ответе — точная подстрока исходного запроса (число вместе со
    словом единицы, если оно рядом) — по ней attribute_extractor вырезает
    найденное из clean_text; сам текст ответа LLM для этого не
    используется, только её структурированные value/unit/raw построчно."""
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


def extract_brand_with_yandexgpt(raw_query: str, client=None, model: str | None = None) -> BrandGuess:
    """Слой 0 (см. brand_extractor.py): в отличие от attribute_extractor.py
    (числовые атрибуты, закрытый словарь единиц измерения) — бренд не
    ограничен заранее известным списком, поэтому только LLM, без
    словарного пути. Извлечённый бренд используется pipeline.py как
    отдельный, приоритетный поисковый термин ("Пульсар частотный
    преобразователь", бренд первым) — design-обсуждение: в длинном запросе
    со всеми характеристиками бренд может теряться в ранжировании самого
    внешнего источника поиска, отдельный короткий запрос с брендом даёт
    источнику независимый шанс найти то же самое иначе."""
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


def extract_contacts_with_yandexgpt(site_text: str, client=None, model: str | None = None) -> ContactGuess:
    """Слой 3 (см. relevance_llm.extract_contacts): запасной вариант к
    regex-извлечению контактов (sources/base.py PHONE_RE/EMAIL_RE/
    ADDRESS_RE, вызывается в pipeline._attach_site_contacts) — тратится
    только на поля, которые regex не нашёл ни на одной из скачанных
    страниц сайта. У regex нет "словаря" форматов, как у единиц измерения
    (в отличие от attribute_extractor.py) — контакты пишут как угодно,
    поэтому здесь LLM реально может найти то, что не покрыл ни один
    вариант паттерна."""
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
