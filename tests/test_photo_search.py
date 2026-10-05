"""Тесты модуля поиска по фото (photo_search.py) — без реальной сети.

Мультимодальная модель (yandexgpt_classifier._ask_image) мокается, чтобы тесты
были быстрыми и изолированными.
"""

from procurement_search.photo_search import (
    MAX_IMAGE_BYTES,
    describe_product_from_image,
)


class _FakeGuess:
    def __init__(self, keywords: str = "Кабель ВВГ 3х2,5"):
        self.keywords = keywords


def _fake_ask(**kwargs):
    return _FakeGuess()


# --- успешное описание товара по фото -------------------------------------

def test_describe_returns_keywords_on_success(monkeypatch):
    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier._ask_image", _fake_ask
    )
    result = describe_product_from_image(b"fake-jpeg", "image/jpeg")
    assert result == "Кабель ВВГ 3х2,5"


def test_describe_passes_card_and_params_to_multimodal(monkeypatch):
    """Картинка и параметры уходят в _ask_image; возвращается keywords."""
    captured = {}

    def _spy(**kwargs):
        captured["kwargs"] = kwargs
        return _FakeGuess("Насос дренажный")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier._ask_image", _spy
    )

    result = describe_product_from_image(b"jpegbytes", "image/jpeg")
    assert result == "Насос дренажный"
    assert captured["kwargs"]["image_bytes"] == b"jpegbytes"
    assert captured["kwargs"]["mimetype"] == "image/jpeg"
    assert captured["kwargs"]["max_tokens"] is not None  # передаём запас токенов


def test_describe_empty_keywords_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier._ask_image",
        lambda **k: _FakeGuess("   "),
    )
    assert describe_product_from_image(b"jpeg", "image/jpeg") == ""


# --- защитные ветки --------------------------------------------------------

def test_describe_empty_bytes_returns_empty(monkeypatch):
    got = {"called": False}

    def _ask(**kwargs):
        got["called"] = True
        return None

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier._ask_image", _ask
    )
    assert describe_product_from_image(b"", "image/jpeg") == ""
    assert got["called"] is False


def test_describe_too_large_returns_empty(monkeypatch):
    got = {"called": False}

    def _ask(**kwargs):
        got["called"] = True
        return None

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier._ask_image", _ask
    )
    result = describe_product_from_image(b"x" * (MAX_IMAGE_BYTES + 1), "image/jpeg")
    assert result == ""
    assert got["called"] is False  # большой файл не уходит в модель


def test_describe_bad_mimetype_returns_empty(monkeypatch):
    got = {"called": False}

    def _ask(**kwargs):
        got["called"] = True
        return None

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier._ask_image", _ask
    )
    assert describe_product_from_image(b"hello", "text/plain") == ""
    assert got["called"] is False


def test_describe_llm_error_returns_empty(monkeypatch):
    def _ask(**kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(
        "procurement_search.yandexgpt_classifier._ask_image", _ask
    )
    assert describe_product_from_image(b"jpeg", "image/jpeg") == ""