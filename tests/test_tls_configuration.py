"""Tests for configurable TLS verification."""

from __future__ import annotations

import asyncio
import ssl
import threading

from custom_components.loxone.pyloxone_api.connection import LoxoneConnection
from custom_components.loxone.pyloxone_api.loxone_http_client import (
    LoxoneAsyncHttpClient,
)


class _Response:
    status = 200


class _Session:
    closed = False

    def __init__(self) -> None:
        self.url = None
        self.kwargs = None

    async def get(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return _Response()


def test_https_http_client_can_disable_certificate_verification() -> None:
    session = _Session()
    client = LoxoneAsyncHttpClient(
        "192.0.2.1",
        "user",
        "password",
        scheme="https",
        verify_ssl=False,
        session=session,
    )

    asyncio.run(client.get("/jdev/cfg/apiKey"))

    assert session.url == "https://192.0.2.1/jdev/cfg/apiKey"
    assert session.kwargs["ssl"] is False


def test_http_client_does_not_pass_tls_options_for_plain_http() -> None:
    session = _Session()
    client = LoxoneAsyncHttpClient(
        "192.0.2.1:8080",
        "user",
        "password",
        scheme="http",
        verify_ssl=False,
        session=session,
    )

    asyncio.run(client.get("/jdev/cfg/apiKey"))

    assert "ssl" not in session.kwargs


def test_websocket_uses_unverified_context_only_when_requested() -> None:
    connection = LoxoneConnection(
        host="192.0.2.1",
        port=443,
        username="user",
        password="password",
        verify_ssl=False,
    )

    ssl_context = asyncio.run(connection._async_websocket_ssl_context())

    assert ssl_context is not None
    assert ssl_context.check_hostname is False
    assert ssl_context.verify_mode == ssl.CERT_NONE


def test_websocket_builds_verified_context_outside_event_loop(monkeypatch) -> None:
    context_threads = []

    def create_default_context() -> ssl.SSLContext:
        context_threads.append(threading.current_thread())
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(ssl, "create_default_context", create_default_context)
    connection = LoxoneConnection(
        host="192.0.2.1",
        port=443,
        username="user",
        password="password",
    )

    ssl_context = asyncio.run(connection._async_websocket_ssl_context())

    assert ssl_context is not None
    assert ssl_context.check_hostname is True
    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert context_threads
    assert context_threads[0] is not threading.main_thread()
