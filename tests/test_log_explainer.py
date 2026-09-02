"""Тесты log_explainer.py — без сети и без LLM, чистый детерминированный
паттерн-матчинг над logging.LogRecord."""

import logging

from procurement_search.log_explainer import ExplainingLogHandler


def _make_logger(handler: ExplainingLogHandler) -> logging.Logger:
    logger = logging.getLogger("test_log_explainer")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger


def test_known_message_pattern_prints_explanation(capsys):
    handler = ExplainingLogHandler()
    logger = _make_logger(handler)

    logger.warning("Кандидатов не найдено — ни один источник не настроен")

    out = capsys.readouterr().out
    assert "Ни один источник поиска не настроен" in out
    assert "GOOGLE_CSE_API_KEY" in out


def test_unknown_api_key_pattern_prints_explanation(capsys):
    """Регрессия по реальному кейсу: обратный слэш перед '_' в .env
    (артефакт копирования) сделал ключ невалидным — Yandex Cloud ответил
    401 'Unknown api key', отдельная ошибка от уже покрытой 403
    PermissionDenied (там ключ валиден, но не тот каталог)."""
    handler = ExplainingLogHandler()
    logger = _make_logger(handler)

    logger.warning(
        "LLM-классификация категории не сработала: Error code: 401 - "
        "{'error': {'message': \"rpc error: code = Unauthenticated desc = "
        "Unknown api key 'AQVN****khr7 (219C20C9)'\"}}"
    )

    out = capsys.readouterr().out
    assert "неизвестный API-ключ" in out


def test_known_exception_text_is_inspected_even_if_message_is_generic(capsys):
    """Реальный кейс: relevance_llm.py логирует обобщённое сообщение
    ('LLM-проверка ... не сработала'), а настоящая причина ('Требуется
    YANDEX_FM_API_KEY') видна только в тексте самого исключения
    (exc_info=True) — паттерн должен сработать и по нему тоже."""
    handler = ExplainingLogHandler()
    logger = _make_logger(handler)

    try:
        raise RuntimeError(
            "Требуется YANDEX_FM_API_KEY (API-ключ Yandex AI Studio — не путать с "
            "YANDEX_SEARCH_API_KEY, это другой сервис Yandex Cloud)"
        )
    except RuntimeError:
        logger.warning("LLM-проверка релевантности для запроса 'x' не сработала", exc_info=True)

    out = capsys.readouterr().out
    assert "Не задан YANDEX_FM_API_KEY" in out


def test_unknown_warning_produces_no_output(capsys):
    handler = ExplainingLogHandler()
    logger = _make_logger(handler)

    logger.warning("Совершенно незнакомое предупреждение, которого нет в словаре")

    out = capsys.readouterr().out
    assert out == ""


def test_info_level_records_are_ignored(capsys):
    """Handler слушает только WARNING и выше (см. level=logging.WARNING по
    умолчанию) — INFO-логи (их в проекте много, включая обычный прогресс
    поиска) не должны триггерить объяснения."""
    handler = ExplainingLogHandler()
    logger = _make_logger(handler)

    logger.info("Кандидатов не найдено — ни один источник не настроен")

    out = capsys.readouterr().out
    assert out == ""


def test_same_issue_explained_only_once_per_handler_lifetime(capsys):
    """SSL-блокировка на 15 разных URL одного краулинга не должна напечатать
    одно и то же объяснение 15 раз подряд — только один раз за время жизни
    хендлера (= один раз за запуск процесса)."""
    handler = ExplainingLogHandler()
    logger = _make_logger(handler)

    for _ in range(5):
        logger.warning("Ошибка запроса к https://x.example: SSLError(...)")

    out = capsys.readouterr().out
    assert out.count("Сайт/сервис оборвал соединение") == 1


def test_different_issues_each_explained_once(capsys):
    handler = ExplainingLogHandler()
    logger = _make_logger(handler)

    logger.warning("Ошибка запроса к https://x.example: SSLError(...)")
    logger.warning("Dadata suggest не сработал для 'ООО Ромашка'")

    out = capsys.readouterr().out
    assert "Сайт/сервис оборвал соединение" in out
    assert "Dadata suggest недоступна" in out
