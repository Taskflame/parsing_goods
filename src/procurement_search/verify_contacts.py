"""Контроль актуальности контактов (ТЗ п.4, design_doc §8).

Design_doc §8 описывает полную цепочку из четырёх проверок: MX-запись
домена → HTTP-статус сайта → сверка реквизитов в футере → SMTP-проверка
почты без отправки письма. Здесь реализована честная часть этой цепочки —
**HTTP-статус сайта**: если сайт компании не отвечает (домен не резолвится,
сервер не отвечает, таймаут) — это сильный сигнал, что компания могла
прекратить деятельность или сменить контакты, и стоит пометить это явно, а
не тихо показать протухший адрес как надёжный. MX/футер/SMTP-проверки не
реализованы — не переоцениваем то, что не сделано.

Важный нюанс из практики (см. историю с 503 от pulscen.ru/optlist.ru):
любой полученный HTTP-ответ, даже с кодом ошибки (403, 404, 503), означает,
что сервер на домене отвечает — то есть домен, скорее всего, жив, просто
либо блокирует автоматических клиентов, либо у него technical issue на этой
конкретной странице. "Протух" здесь означает конкретно "не отвечает на
уровне соединения" (DNS не резолвится, connection refused, таймаут) — не
"вернул код ошибки". Это осознанно консервативная трактовка: не хотим
помечать сайт как мёртвый только потому, что антибот-защита не пустила наш
скрипт, — ложное "протух" вреднее, чем его отсутствие.

Тот же принцип распространяется на обрыв TLS-рукопожатия (design-
обсуждение: проверено вживую curl-ом на реальных сайтах — TLS-фингерпринт
скрипта отличают от браузера и обрывают соединение SSL_ERROR_SYSCALL'ом ещё
до HTTP-ответа, при том что сайт прекрасно открывается в браузере).
`requests.exceptions.SSLError` — обрыв ПОСЛЕ того, как TCP-соединение уже
установилось и начался TLS-хендшейк, то есть сервер на этом порту реально
есть — надёжный признак "антибот", а не "домена нет". Трактуется как
UNVERIFIED (нет данных), а не STALE — та же дисциплина "нет данных, не
штраф", что и везде в scoring.py.
"""

from __future__ import annotations

import logging

import requests

from procurement_search.models import VerificationFlag

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def check_website_liveness(
    url: str | None,
    timeout: float = 5.0,
    session: requests.Session | None = None,
) -> VerificationFlag:
    """HEAD-запрос на сайт компании. UNVERIFIED, если url не задан (нечего
    проверять — не выдумываем результат) или соединение оборвалось уже на
    TLS-рукопожатии (см. докстринг модуля — почти всегда антибот-защита по
    TLS-фингерпринту, а не мёртвый домен); CONFIRMED на любой полученный
    HTTP-ответ; STALE — на ошибку уровня соединения ДО TLS (DNS не
    резолвится, connection refused, таймаут)."""
    if not url:
        return VerificationFlag.UNVERIFIED

    session = session or requests.Session()
    try:
        session.head(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
    except requests.exceptions.SSLError:
        logger.info(
            "Сайт %s оборвал TLS-рукопожатие (похоже на антибот-защиту по "
            "TLS-фингерпринту, не на мёртвый домен) — контакты остаются 'не проверен'",
            url,
        )
        return VerificationFlag.UNVERIFIED
    except requests.RequestException:
        logger.info("Сайт %s не отвечает — контакты помечаются как 'протух'", url)
        return VerificationFlag.STALE
    return VerificationFlag.CONFIRMED