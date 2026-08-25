"""Тесты verify_contacts.py — без реальной сети, HTTP-сессия подменяется."""

import requests

from procurement_search.models import VerificationFlag
from procurement_search.verify_contacts import check_website_liveness


class _RespondingSession:
    """Имитирует сайт, который отвечает (неважно каким кодом — важно, что
    соединение состоялось)."""

    def head(self, url, timeout=None, allow_redirects=None, headers=None):
        class _Resp:
            status_code = 200

        return _Resp()


class _UnreachableSession:
    def head(self, *args, **kwargs):
        raise requests.ConnectionError("домен не резолвится")


class _TlsBlockedSession:
    """Имитирует антибот-защиту по TLS-фингерпринту: TCP+TLS начались (сервер
    реально есть), но соединение оборвано во время рукопожатия — до HTTP-ответа."""

    def head(self, *args, **kwargs):
        raise requests.exceptions.SSLError("SSL_ERROR_SYSCALL")


def test_no_url_returns_unverified():
    assert check_website_liveness(None) == VerificationFlag.UNVERIFIED
    assert check_website_liveness("") == VerificationFlag.UNVERIFIED


def test_responding_site_is_confirmed():
    result = check_website_liveness("https://example.com", session=_RespondingSession())
    assert result == VerificationFlag.CONFIRMED


def test_unreachable_site_is_stale():
    result = check_website_liveness("https://dead-domain.example", session=_UnreachableSession())
    assert result == VerificationFlag.STALE


def test_tls_handshake_blocked_is_unverified_not_stale():
    result = check_website_liveness("https://anti-bot-protected.example", session=_TlsBlockedSession())
    assert result == VerificationFlag.UNVERIFIED