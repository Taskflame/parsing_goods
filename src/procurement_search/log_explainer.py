"""Вариант А (design-обсуждение) — детерминированный, БЕЗ LLM объяснитель
логов: подключается к корневому логгеру при старте CLI/webapp
(`cli.py`/`webapp.py`) и для уже ЗНАКОМЫХ по этому проекту ошибок печатает
человеческое объяснение + совет прямо в консоль, поверх обычного лога (не
заменяет его, а дополняет).

ПОЧЕМУ БЕЗ LLM: сознательный выбор, а не первый шаг к чему-то большему.
Основные ошибки в этом проекте — конечный, небольшой и уже известный
набор (нет ключа в .env, сайт заблокировал соединение, не установлен
опциональный пакет) — для них не нужна генеративная модель, обычный
словарь паттернов дешевле, быстрее и не может "довраться". LLM-версия
(вариант Б, см. design-обсуждение) была бы к тому же логически хрупкой
именно там, где нужнее всего: если ошибка — "YandexGPT недоступен",
объяснять её тем же самым сломанным LLM-вызовом нельзя.

Смотрит не только на текст сообщения (`record.getMessage()`), но и на
текст самого исключения (`record.exc_info`), если оно было передано через
`exc_info=True` (см. enrichment.py/relevance_llm.py) — иначе, например,
настоящая причина 'Требуется YANDEX_FM_API_KEY' видна только в трейсбеке
исключения, а не в тексте самого warning-сообщения, которое чаще звучит
обобщённо ('LLM-проверка ... не сработала')."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _KnownIssue:
    slug: str  # для дедупликации — не печатаем одно и то же объяснение много раз за запуск
    pattern: re.Pattern
    title: str
    explanation: str
    fix: str


_KNOWN_ISSUES: list[_KnownIssue] = [
    _KnownIssue(
        slug="ssl_blocked",
        pattern=re.compile(r"SSLError|SSLEOFError|ERR_CONNECTION_CLOSED|ConnectionError"),
        title="Сайт/сервис оборвал соединение (SSL/сеть)",
        explanation=(
            "Удалённый сервер закрыл TLS-соединение до ответа — типично для блокировки "
            "по IP-диапазону (датацентр/облако вместо обычного домашнего/офисного интернета), "
            "не связано с кодом проекта."
        ),
        fix=(
            "Если это происходит с САЙТОМ КАНДИДАТА — ожидаемо, пайплайн просто пропустит "
            "этого кандидата (см. docs/design_doc.md §11). Если это происходит с DADATA "
            "или другим API — проверьте доступность сети/прокси до этого хоста."
        ),
    ),
    _KnownIssue(
        slug="dadata_failed",
        pattern=re.compile(r"Dadata suggest не сработал"),
        title="Dadata suggest недоступна",
        explanation=(
            "Резолвинг компании в ЕГРЮЛ через Dadata не сработал — сеть, дневной лимит "
            "бесплатного тарифа, или неверный ключ."
        ),
        fix="Проверьте DADATA_API_KEY в .env и лимиты на dadata.ru — без резолвинга компания останется со status='неизвестно', это не критично для остального пайплайна.",
    ),
    _KnownIssue(
        slug="missing_fm_api_key",
        pattern=re.compile(r"YANDEX_FM_API_KEY"),
        title="Не задан YANDEX_FM_API_KEY",
        explanation="LLM-вызовы (релевантность/цена/наличие/контакты и т.п.) требуют ключ Yandex AI Studio — он не задан или неверен.",
        fix="Добавьте YANDEX_FM_API_KEY=... в .env (это НЕ тот же ключ, что YANDEX_SEARCH_API_KEY — другой сервис Yandex Cloud).",
    ),
    _KnownIssue(
        slug="missing_fm_model",
        pattern=re.compile(r"YANDEX_FM_MODEL"),
        title="Не задана модель Yandex AI Studio",
        explanation="Не хватает YANDEX_FM_MODEL и/или folder_id для сборки адреса модели.",
        fix="Добавьте YANDEX_FM_MODEL=yandexgpt и, если ключ AI Studio из другого аккаунта, чем поиск — YANDEX_FM_FOLDER_ID в .env.",
    ),
    _KnownIssue(
        slug="playwright_missing",
        pattern=re.compile(r"playwright не установлен"),
        title="Playwright не установлен (--probe-stepper)",
        explanation="Пилот интерактивной проверки степпера количества (stepper_probe.py) требует отдельно установленный браузерный движок.",
        fix="pip install playwright && playwright install chromium — не входит в requirements.txt намеренно (тяжёлая опциональная зависимость).",
    ),
    _KnownIssue(
        slug="openai_missing",
        pattern=re.compile(r"[Пп]акет openai не установлен"),
        title="Пакет openai не установлен",
        explanation="LLM-вызовы (yandexgpt_classifier.py) используют OpenAI-совместимый SDK — он не найден в окружении.",
        fix="pip install -r requirements.txt (openai уже в списке зависимостей — проверьте, что вы в нужном virtualenv).",
    ),
    _KnownIssue(
        slug="yandex_403_permission_denied",
        pattern=re.compile(r"403.{0,40}(PermissionDenied|Forbidden)|(PermissionDenied|Forbidden).{0,40}403"),
        title="403 PermissionDenied от Yandex Cloud",
        explanation=(
            "API-ключ и folder ID не совпадают — ключ выпущен в ДРУГОМ каталоге, чем тот, "
            "что указан в YANDEX_FOLDER_ID/YANDEX_FM_FOLDER_ID (или у ключа нет прав на "
            "нужный сервис в этом каталоге)."
        ),
        fix=(
            "Проверьте, что YANDEX_SEARCH_API_KEY/YANDEX_FM_API_KEY реально выпущены в том "
            "же каталоге, что указан в YANDEX_FOLDER_ID/YANDEX_FM_FOLDER_ID в .env — при "
            "смене одного из них без другого 403 ожидаем, нужен либо новый ключ под этот "
            "каталог, либо вернуть прежний folder ID."
        ),
    ),
    _KnownIssue(
        slug="yandex_unknown_api_key",
        pattern=re.compile(r"Unknown api key|AuthenticationError"),
        title="401 Unauthorized: неизвестный API-ключ Yandex Cloud",
        explanation=(
            "Yandex Cloud не узнаёт значение YANDEX_SEARCH_API_KEY/YANDEX_FM_API_KEY вообще "
            "— не вопрос каталога (это была бы отдельная 403-ошибка), сам ключ невалиден. "
            "Частая причина: лишние символы при копировании (например, обратный слэш "
            "перед подчёркиванием — экранирование из Markdown/чата, попавшее в .env как есть)."
        ),
        fix=(
            "Откройте .env и сверьте ключ посимвольно с тем, что показывает консоль Yandex "
            "Cloud — особенно на предмет случайно вставленных `\\` перед `_`/иных спецсимволов."
        ),
    ),
    _KnownIssue(
        slug="no_candidates",
        pattern=re.compile(r"Кандидатов не найдено"),
        title="Ни один источник поиска не настроен/недоступен",
        explanation="Google CSE, Yandex Search и Yandex gen-search — все три либо без ключей в .env, либо недоступны из этой сети.",
        fix="Проверьте GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX или YANDEX_SEARCH_API_KEY/YANDEX_FOLDER_ID в .env — нужен хотя бы один источник.",
    ),
    _KnownIssue(
        slug="check_availability_no_deep_relevance",
        pattern=re.compile(r"check_availability=True.*требует deep_relevance=True"),
        title="check_availability включён без --deep-relevance",
        explanation="Слою 4 нечего анализировать без текста сайта, который скачивает Слой 2.",
        fix="Включите --deep-relevance (или чекбокс «Слой 2») вместе с check_availability.",
    ),
    _KnownIssue(
        slug="probe_stepper_no_check_availability",
        pattern=re.compile(r"probe_stepper=True требует check_availability=True"),
        title="probe_stepper включён без check_availability",
        explanation="Пробинг степпера уточняет только неясные вердикты Слоя 4 — без него не от чего оттолкнуться.",
        fix="Включите --check-availability (и --deep-relevance) вместе с --probe-stepper.",
    ),
]


class ExplainingLogHandler(logging.Handler):
    """Подключается к корневому логгеру (см. cli.py/webapp.py) — слушает
    WARNING и выше со ВСЕХ логгеров проекта разом (logging.getLogger(__name__)
    в каждом модуле пробрасывает записи вверх по иерархии до root, если не
    настроено обратное). Одна и та же категория проблемы объясняется не
    больше одного раза за запуск процесса (self._explained) — иначе,
    например, SSL-блокировка на 15 разных URL одного краулинга напечатала
    бы одно и то же объяснение 15 раз подряд."""

    def __init__(self, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self._explained: set[str] = set()

    def emit(self, record: logging.LogRecord) -> None:
        haystack = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            haystack = f"{haystack} {record.exc_info[1]}"

        for issue in _KNOWN_ISSUES:
            if issue.slug in self._explained:
                continue
            if issue.pattern.search(haystack):
                self._explained.add(issue.slug)
                self._print_explanation(issue)
                return  # первое совпадение побеждает — не проверяем остальные паттерны

    def _print_explanation(self, issue: _KnownIssue) -> None:
        print(
            f"\n--- Пояснение: {issue.title} ---\n"
            f"Почему: {issue.explanation}\n"
            f"Что делать: {issue.fix}\n"
            f"---\n"
        )
