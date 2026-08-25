"""Тесты cloudru_classifier.py — без сети: OpenAI-совместимый клиент
подменяется фейком, возвращающим заготовленный JSON в
response.choices[0].message.content (тот же формат, что у настоящего
`openai`/`evolution-openai` клиента)."""

import pytest

from procurement_search.cloudru_classifier import (
    classify_attribute_match_with_cloudru,
    classify_attributes_batch_with_cloudru,
    classify_listing_type_with_cloudru,
    classify_relevance_with_cloudru,
    classify_stock_status_with_cloudru,
    extract_brand_with_cloudru,
    extract_contacts_with_cloudru,
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



def test_classify_relevance_with_cloudru_parses_verdict():
    client = _FakeClient('{"is_relevant": true, "reasoning": "товар в каталоге"}')

    result = classify_relevance_with_cloudru(
        "гальванические покрытия", "у нас есть цинкование", client=client, model="test-model"
    )

    assert result.is_relevant is True


def test_classify_attribute_match_with_cloudru_parses_mismatch():
    client = _FakeClient(
        '{"matches": false, "mismatches": ["производительность"], "reasoning": "18 л/ч vs 10000 л/час"}'
    )

    result = classify_attribute_match_with_cloudru(
        "дренажный насос 10000 л/час", "насос 18 л/ч", client=client, model="test-model"
    )

    assert result.matches is False
    assert result.mismatches == ["производительность"]


def test_classify_listing_type_with_cloudru_parses_verdict():
    client = _FakeClient('{"is_listing": false, "reasoning": "статья, не карточка товара"}')

    result = classify_listing_type_with_cloudru(
        "бензогенератор FinePower", "Как работает генератор", "обзор технологий", client=client, model="test-model"
    )

    assert result.is_listing is False


def test_classify_attributes_batch_with_cloudru_parses_units():
    client = _FakeClient(
        '{"attributes": ['
        '{"value": "5.5", "unit": "квт", "raw": "5,5 киловат", "reasoning": "мощность, опечатка в киловатт"},'
        '{"value": "2024", "unit": null, "raw": "2024", "reasoning": "похоже на год, не характеристика"}'
        ']}'
    )

    result = classify_attributes_batch_with_cloudru(
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


def test_classify_stock_status_with_cloudru_parses_verdict():
    client = _FakeClient('{"out_of_stock": true, "reasoning": "плашка \'товар закончился\'"}')

    result = classify_stock_status_with_cloudru(
        "Насос дренажный. Товар закончился.", client=client, model="test-model"
    )

    assert result.out_of_stock is True


def test_ask_json_sends_json_schema_matching_output_model():
    client = _FakeClient('{"is_listing": true, "reasoning": "ok"}')

    classify_listing_type_with_cloudru("запрос", "заголовок", "сниппет", client=client, model="test-model")

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
    monkeypatch.setattr("procurement_search.cloudru_classifier.CLOUDRU_MODEL", None)
    client = _FakeClient('{"is_listing": true, "reasoning": "ok"}')

    with pytest.raises(RuntimeError, match="CLOUDRU_MODEL"):
        classify_listing_type_with_cloudru("запрос", "заголовок", "сниппет", client=client)


def test_raises_without_credentials_when_client_not_provided(monkeypatch):
    monkeypatch.delenv("CLOUDRU_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="CLOUDRU_API_KEY"):
        classify_listing_type_with_cloudru("запрос", "заголовок", "сниппет", model="test-model")


def test_extract_brand_with_cloudru_parses_brand():
    client = _FakeClient('{"brand": "Пульсар", "reasoning": "явно указан бренд"}')

    result = extract_brand_with_cloudru(
        "частотный преобразователь пульсар 5,5 киловат 380 вольт", client=client, model="test-model"
    )

    assert result.brand == "Пульсар"


def test_extract_contacts_with_cloudru_parses_partial_result():
    client = _FakeClient(
        '{"phone": "+7 (495) 256-16-36", "email": null, "address": null, "reasoning": "только телефон найден"}'
    )

    result = extract_contacts_with_cloudru("контакты компании...", client=client, model="test-model")

    assert result.phone == "+7 (495) 256-16-36"
    assert result.email is None
    assert result.address is None
