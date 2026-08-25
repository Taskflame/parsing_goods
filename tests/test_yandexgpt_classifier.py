"""Тесты yandexgpt_classifier.py — без сети: OpenAI-совместимый клиент
подменяется фейком, возвращающим заготовленный JSON в
response.choices[0].message.content (тот же формат, что и у
cloudru_classifier.py, см. test_cloudru_classifier.py)."""

import pytest

from procurement_search.yandexgpt_classifier import (
    classify_attribute_match_with_yandexgpt,
    classify_attributes_batch_with_yandexgpt,
    classify_listing_type_with_yandexgpt,
    classify_relevance_with_yandexgpt,
    classify_stock_status_with_yandexgpt,
    extract_brand_with_yandexgpt,
    extract_contacts_with_yandexgpt,
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
    client = _FakeClient('{"out_of_stock": true, "reasoning": "плашка \'товар закончился\'"}')

    result = classify_stock_status_with_yandexgpt(
        "Насос дренажный. Товар закончился.", client=client, model="test-model"
    )

    assert result.out_of_stock is True


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
