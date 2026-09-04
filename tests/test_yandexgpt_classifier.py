"""Тесты yandexgpt_classifier.py — без сети: OpenAI-совместимый клиент
подменяется фейком, возвращающим заготовленный JSON в
response.choices[0].message.content."""

import pytest

from procurement_search.yandexgpt_classifier import (
    classify_attribute_match_with_yandexgpt,
    classify_attributes_batch_with_yandexgpt,
    classify_category_with_yandexgpt,
    classify_listing_type_with_yandexgpt,
    classify_relevance_with_yandexgpt,
    classify_stock_status_with_yandexgpt,
    condense_query_with_yandexgpt,
    extract_availability_with_yandexgpt,
    extract_brand_with_yandexgpt,
    extract_contacts_with_yandexgpt,
    extract_legal_name_with_yandexgpt,
    extract_price_with_yandexgpt,
)


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content: str):
        self._content = content
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content: str):
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content: str):
        self.chat = _FakeChat(content)



def test_classify_relevance_with_yandexgpt_parses_verdict():
    client = _FakeClient('{"is_relevant": true, "reasoning": "товар в каталоге"}')

    result = classify_relevance_with_yandexgpt(
        "гальванические покрытия", "у нас есть цинкование", client=client, model="test-model"
    )

    assert result.is_relevant is True


def test_classify_attribute_match_with_yandexgpt_parses_mismatch():
    client = _FakeClient(
        '{"matches": false, "mismatches": ["производительность"], "reasoning": "18 л/ч vs 10000 л/час"}'
    )

    result = classify_attribute_match_with_yandexgpt(
        "дренажный насос 10000 л/час", "насос 18 л/ч", client=client, model="test-model"
    )

    assert result.matches is False
    assert result.mismatches == ["производительность"]


def test_classify_listing_type_with_yandexgpt_parses_verdict():
    client = _FakeClient('{"is_listing": false, "reasoning": "статья, не карточка товара"}')

    result = classify_listing_type_with_yandexgpt(
        "бензогенератор FinePower", "Как работает генератор", "обзор технологий", client=client, model="test-model"
    )

    assert result.is_listing is False


def test_classify_attributes_batch_with_yandexgpt_parses_units():
    client = _FakeClient(
        '{"attributes": ['
        '{"value": "5.5", "unit": "квт", "raw": "5,5 киловат", "reasoning": "мощность, опечатка в киловатт"},'
        '{"value": "2024", "unit": null, "raw": "2024", "reasoning": "похоже на год, не характеристика"}'
        ']}'
    )

    result = classify_attributes_batch_with_yandexgpt(
        "частотный преобразователь 5,5 киловат, модель 2024",
        ["5,5", "2024"],
        {"квт": ["квт", "киловатт"], "в": ["в", "вольт"]},
        client=client,
        model="test-model",
    )

    assert [(a.value, a.unit, a.raw) for a in result.attributes] == [
        ("5.5", "квт", "5,5 киловат"),
        ("2024", None, "2024"),
    ]
    response_format = client.chat.completions.calls[0]["response_format"]
    assert response_format["json_schema"]["name"] == "AttributeBatchGuess"
    user_message = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "5,5" in user_message and "2024" in user_message


def test_classify_stock_status_with_yandexgpt_parses_verdict():
    client = _FakeClient(
        '{"status": "out_of_stock", "quote": "Товар закончился", '
        '"reasoning": "плашка \'товар закончился\'"}'
    )

    result = classify_stock_status_with_yandexgpt(
        "Насос дренажный. Товар закончился.", client=client, model="test-model"
    )

    assert result.status == "out_of_stock"
    assert result.quote == "Товар закончился"


def test_classify_stock_status_prompt_does_not_treat_cart_button_as_in_stock():
    """Регрессия по реальному кейсу с живой выдачи (san-sanych.ru): страница
    показывала «Ожидается поставка на 31.08» рядом с активной кнопкой «В
    корзину» — старый промпт явно называл кнопку триггером in_stock, из-за
    чего модель игнорировала более специфичный маркер срока поставки.
    Кнопка сама по себе НЕ должна быть в списке триггеров in_stock, и
    промпт должен явно требовать приоритет маркера срока/даты поставки."""
    client = _FakeClient('{"status": "in_stock", "quote": null, "reasoning": "тест"}')

    classify_stock_status_with_yandexgpt("текст сайта", client=client, model="test-model")

    system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "кнопка" not in system_prompt.lower().split("важно")[0]
    assert "перевешивает" in system_prompt or "приоритет" in system_prompt


def test_extract_availability_with_yandexgpt_parses_on_order():
    """on_order — отдельный статус именно для случая 'товара нет на складе,
    но есть дата/срок будущей поставки' (см. models.AvailabilityStatus).
    Регрессия: старый промпт вообще не объяснял модели, что означает
    on_order, и не отличал его от unknown/in_stock."""
    client = _FakeClient(
        '{"is_product_page": true, "status": "on_order", "quantity": null, '
        '"quantity_unit": null, "pack_size_qty": null, "pack_size_unit": null, '
        '"min_order_qty": null, "min_order_unit": null, "lead_time_days": null, '
        '"price": "1 051,03 ₽", "evidence": "Ожидается поставка на 31.08", '
        '"reasoning": "явный маркер срока поставки"}'
    )

    result = extract_availability_with_yandexgpt(
        "труба стальная черная 3/4", "В корзину. Ожидается поставка на 31.08.",
        client=client, model="test-model",
    )

    assert result.status == "on_order"
    assert result.evidence == "Ожидается поставка на 31.08"

    system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "on_order" in system_prompt
    assert "кнопка" in system_prompt.lower()


def test_extract_availability_prompt_warns_against_picking_one_pickup_point():
    """Регрессия по реальному кейсу с живой выдачи (pro-electro.ru):
    остаток показан отдельно по нескольким точкам самовывоза (несколько
    строк 'город, адрес — N шт'), а модель взяла число только с одной
    строки как итоговое — теряя остальные точки. Промпт должен явно
    требовать сумму по всем видимым точкам или честный null при
    неуверенности, а не произвольный выбор одной строки."""
    client = _FakeClient(
        '{"is_product_page": true, "status": "in_stock", "quantity": null, '
        '"quantity_unit": null, "pack_size_qty": null, "pack_size_unit": null, '
        '"min_order_qty": null, "min_order_unit": null, "lead_time_days": null, '
        '"price": null, "evidence": "остаток по точкам самовывоза, полный список не виден", '
        '"reasoning": "несколько точек самовывоза"}'
    )

    extract_availability_with_yandexgpt(
        "бензогенератор Huter", "г. Томск ул. X — 1 шт г. Томск ул. Y — 1 шт",
        client=client, model="test-model",
    )

    system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "самовывоз" in system_prompt.lower()
    assert "просумм" in system_prompt.lower()


def test_condense_query_with_yandexgpt_parses_kernel_and_region():
    """LLM сжимает длинный шаблонный запрос до короткого ядра + региона."""
    client = _FakeClient(
        '{"kernel": "обращение с отходами III-IV классов опасности в Чувашии", '
        '"region": "Чувашия", "reasoning": "убрал лишние цитаты и статью"}'
    )

    result = condense_query_with_yandexgpt(
        "ищу компанию в Чувашии по услугам по обращению с отходами...",
        client=client, model="test-model",
    )

    assert result.kernel == "обращение с отходами III-IV классов опасности в Чувашии"
    assert result.region == "Чувашия"

    system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "сжимаешь" in system_prompt
    assert "ОСТАВЬ" in system_prompt


def test_classify_listing_type_prompt_allows_service_provider_pages():
    """Регрессия: запросы вида 'подрядная организация по изысканиям', где байер
    ищет УСЛУГУ/подрядчика/компанию (а не купить товар), должны допускать
    страницы компаний/услуг — иначе LLM-фильтр типа контента жёстко выкидывает
    всю выдачу и результат пустой. При этом статьи/реестры по-прежнему
    отсекаются. Проверяем, что промпт явно описывает оба правила."""
    client = _FakeClient('{"is_listing": true, "reasoning": "страница компании-исполнителя"}')

    classify_listing_type_with_yandexgpt(
        "ищу подрядную организацию в Татарстане по инженерным изысканиям",
        "ООО ГеоИзыскания", "инженерные изыскания, тел.",
        client=client, model="test-model",
    )

    system_prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    # Подрядчик/услуга допускается
    assert "УСЛУГОВОГО запроса" in system_prompt
    assert "подрядчика" in system_prompt.lower() or "услугу" in system_prompt.lower()
    # Агрегаторы/реестры (НОСТРОЙ, реестр СРО) — нет
    assert "НОСТРОЙ" in system_prompt
    assert "реестр" in system_prompt.lower()


def test_ask_json_sends_json_schema_matching_output_model():
    client = _FakeClient('{"is_listing": true, "reasoning": "ok"}')

    classify_listing_type_with_yandexgpt("запрос", "заголовок", "сниппет", client=client, model="test-model")

    call = client.chat.completions.calls[0]
    system_message = call["messages"][0]
    assert system_message["role"] == "system"

    response_format = call["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "ListingTypeVerdict"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert "is_listing" in response_format["json_schema"]["schema"]["properties"]


def test_raises_without_model_configured(monkeypatch):
    monkeypatch.setattr("procurement_search.yandexgpt_classifier.YANDEX_FM_MODEL", None)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    monkeypatch.delenv("YANDEX_FM_FOLDER_ID", raising=False)
    client = _FakeClient('{"is_listing": true, "reasoning": "ok"}')

    with pytest.raises(RuntimeError, match="YANDEX_FM_MODEL"):
        classify_listing_type_with_yandexgpt("запрос", "заголовок", "сниппет", client=client)


def test_raises_without_credentials_when_client_not_provided(monkeypatch):
    monkeypatch.delenv("YANDEX_FM_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="YANDEX_FM_API_KEY"):
        classify_listing_type_with_yandexgpt("запрос", "заголовок", "сниппет", model="test-model")


def test_default_model_built_from_model_id_and_folder_id(monkeypatch):
    monkeypatch.setattr("procurement_search.yandexgpt_classifier.YANDEX_FM_MODEL", "yandexgpt")
    monkeypatch.delenv("YANDEX_FM_FOLDER_ID", raising=False)
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gexamplefolder")
    client = _FakeClient('{"is_listing": true, "reasoning": "ok"}')

    classify_listing_type_with_yandexgpt("запрос", "заголовок", "сниппет", client=client)

    assert client.chat.completions.calls[0]["model"] == "gpt://b1gexamplefolder/yandexgpt/latest"


def test_default_model_prefers_fm_folder_id_over_shared_one(monkeypatch):
    """Ключ Yandex AI Studio может быть из ДРУГОГО аккаунта, чем
    YANDEX_SEARCH_API_KEY — YANDEX_FM_FOLDER_ID должен побеждать общий
    YANDEX_FOLDER_ID, когда задан явно."""
    monkeypatch.setattr("procurement_search.yandexgpt_classifier.YANDEX_FM_MODEL", "yandexgpt")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gsearchaccountfolder")
    monkeypatch.setenv("YANDEX_FM_FOLDER_ID", "b1gaistudioaccountfolder")
    client = _FakeClient('{"is_listing": true, "reasoning": "ok"}')

    classify_listing_type_with_yandexgpt("запрос", "заголовок", "сниппет", client=client)

    assert client.chat.completions.calls[0]["model"] == "gpt://b1gaistudioaccountfolder/yandexgpt/latest"


def test_extract_brand_with_yandexgpt_parses_brand():
    client = _FakeClient('{"brand": "Пульсар", "reasoning": "явно указан бренд"}')

    result = extract_brand_with_yandexgpt(
        "частотный преобразователь пульсар 5,5 киловат 380 вольт", client=client, model="test-model"
    )

    assert result.brand == "Пульсар"
    response_format = client.chat.completions.calls[0]["response_format"]
    assert response_format["json_schema"]["name"] == "BrandGuess"


def test_extract_brand_with_yandexgpt_returns_none_when_no_brand():
    client = _FakeClient('{"brand": null, "reasoning": "бренд не упомянут"}')

    result = extract_brand_with_yandexgpt("насос дренажный 10000 л/час", client=client, model="test-model")

    assert result.brand is None


def test_extract_price_with_yandexgpt_parses_price():
    client = _FakeClient('{"price": "15 000 руб.", "reasoning": "указана в карточке товара"}')

    result = extract_price_with_yandexgpt("Насос дренажный, цена 15 000 руб.", client=client, model="test-model")

    assert result.price == "15 000 руб."
    response_format = client.chat.completions.calls[0]["response_format"]
    assert response_format["json_schema"]["name"] == "PriceGuess"


def test_extract_price_with_yandexgpt_returns_none_when_no_price():
    client = _FakeClient('{"price": null, "reasoning": "цена по запросу, числа нет"}')

    result = extract_price_with_yandexgpt("Насос дренажный, цена по запросу", client=client, model="test-model")

    assert result.price is None


def test_extract_legal_name_with_yandexgpt_parses_name():
    client = _FakeClient('{"legal_name": "ООО «Диптех»", "reasoning": "указано в футере сайта"}')

    result = extract_legal_name_with_yandexgpt(
        "© 2024 ООО «Диптех». Продаём генераторы Huter.", client=client, model="test-model"
    )

    assert result.legal_name == "ООО «Диптех»"
    response_format = client.chat.completions.calls[0]["response_format"]
    assert response_format["json_schema"]["name"] == "LegalNameGuess"


def test_extract_legal_name_with_yandexgpt_returns_none_when_not_found():
    client = _FakeClient('{"legal_name": null, "reasoning": "юрлицо на странице не указано"}')

    result = extract_legal_name_with_yandexgpt("Каталог товаров", client=client, model="test-model")

    assert result.legal_name is None


_CATEGORIES = {
    "F3": {"name": "СИЛОВОЕ ЭЛЕКТРООБОРУДОВАНИЕ И ДОПОЛНИТЕЛЬНОЕ ОБОРУДОВАНИЕ", "market": "EE"},
    "K1": {"name": "КРЕПЕЖИ", "market": "FC"},
}


def test_classify_category_with_yandexgpt_parses_code():
    client = _FakeClient('{"category": "F3", "reasoning": "генератор — силовое электрооборудование"}')

    result = classify_category_with_yandexgpt(
        "генератор бензиновый Huter 2,5 квт", _CATEGORIES, client=client, model="test-model"
    )

    assert result.category == "F3"
    response_format = client.chat.completions.calls[0]["response_format"]
    assert response_format["json_schema"]["name"] == "CategoryGuess"
    user_message = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "F3" in user_message and "КРЕПЕЖИ" in user_message


def test_classify_category_with_yandexgpt_returns_none_when_nothing_fits():
    client = _FakeClient('{"category": null, "reasoning": "запрос не про закупку товара"}')

    result = classify_category_with_yandexgpt("сколько сейчас времени", _CATEGORIES, client=client, model="test-model")

    assert result.category is None


def test_extract_contacts_with_yandexgpt_parses_partial_result():
    client = _FakeClient(
        '{"phone": "+7 (495) 256-16-36", "email": null, "address": "г. Москва, Кутузовский проспект, 45", '
        '"reasoning": "email в тексте не найден"}'
    )

    result = extract_contacts_with_yandexgpt("контакты компании...", client=client, model="test-model")

    assert result.phone == "+7 (495) 256-16-36"
    assert result.email is None
    assert result.address == "г. Москва, Кутузовский проспект, 45"
    response_format = client.chat.completions.calls[0]["response_format"]
    assert response_format["json_schema"]["name"] == "ContactGuess"
