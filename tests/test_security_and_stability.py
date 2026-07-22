"""Security and connection-lifecycle regression tests."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from types import SimpleNamespace

import aiohttp
import pytest
import voluptuous as vol
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature

import custom_components.loxone.coordinator as coordinator_module
from custom_components.loxone.catalog import feature_is_enabled
from custom_components.loxone.climate import LoxoneAcControl, _json_object_list
from custom_components.loxone import config_flow as config_flow_module
from custom_components.loxone.config_flow import LoxoneFlowHandler, _validate_transport
from custom_components.loxone.const import CONF_ALLOW_INSECURE_HTTP
from custom_components.loxone.coordinator import LoxoneCoordinator
from custom_components.loxone.diagnostics import async_get_config_entry_diagnostics
from custom_components.loxone.fan import LoxoneVentilation
from custom_components.loxone.lights.colorpickers import _parse_color_value
from custom_components.loxone.miniserver import _configuration_url
from custom_components.loxone.pyloxone_api import connection as connection_module
from custom_components.loxone.pyloxone_api.connection import (
    EXTERNAL_CALLBACK_TYPES,
    LoxoneConnection,
)
from custom_components.loxone.pyloxone_api.exceptions import LoxoneTokenError
from custom_components.loxone.pyloxone_api.loxone_http_client import (
    LoxoneAsyncHttpClient,
)
from custom_components.loxone.pyloxone_api.message import MessageType, TextMessage
from custom_components.loxone.pyloxone_api.websocket_protocol import (
    ClientConnection,
    LoxoneClientConnection,
)
from custom_components.loxone.sauna import _as_bool
from custom_components.loxone.system_health import system_health_info


def _connection(**kwargs) -> LoxoneConnection:
    return LoxoneConnection(
        host=kwargs.pop("host", "miniserver.local"),
        port=kwargs.pop("port", 8080),
        username="user",
        password="secret",
        **kwargs,
    )


def test_plain_http_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="allow_insecure_http"):
        _connection(allow_insecure_http=False)

    _validate_transport(
        {
            "host": "miniserver.local",
            "port": 8080,
            CONF_ALLOW_INSECURE_HTTP: True,
        }
    )
    with pytest.raises(vol.Invalid, match="insecure_http"):
        _validate_transport(
            {
                "host": "miniserver.local",
                "port": 8080,
                CONF_ALLOW_INSECURE_HTTP: False,
            }
        )


def test_embedded_url_port_is_preserved() -> None:
    connection = _connection(
        host="https://miniserver.example:8443",
        allow_insecure_http=False,
    )

    assert connection.scheme == "https"
    assert connection.url == "miniserver.example:8443"


def test_untrusted_values_are_not_executed() -> None:
    assert _parse_color_value("temp(50,3200)", "temp", 2) == (50, 3200)
    assert _parse_color_value("temp(True,3200)", "temp", 2) is None
    assert (
        _parse_color_value(
            "temp(__import__('os').system('touch /tmp/loxone-pwned'),3200)",
            "temp",
            2,
        )
        is None
    )


def test_sauna_boolean_parser_handles_textual_zero() -> None:
    assert _as_bool("0") is False
    assert _as_bool("0.0") is False
    assert _as_bool("false") is False
    assert _as_bool("1") is True
    assert _as_bool(0.0) is False
    assert _as_bool(1.0) is True
    assert feature_is_enabled("0") is False
    assert feature_is_enabled("false") is False


def test_command_waits_for_websocket_send_and_propagates_failure() -> None:
    class FakeWebSocket:
        def __init__(self, error: Exception | None = None) -> None:
            self.state = SimpleNamespace(name="OPEN")
            self.error = error
            self.sent = []

        async def send(self, value) -> None:
            if self.error:
                raise self.error
            self.sent.append(value)

    async def successful_send() -> None:
        connection = _connection()
        websocket = FakeWebSocket()
        connection.connection = websocket
        processor = asyncio.create_task(connection._process_message())
        await connection.send_websocket_command("switch-uuid", "on")
        connection._shutdown_event.set()
        await processor
        assert websocket.sent

    async def failed_send() -> None:
        connection = _connection()
        connection.connection = FakeWebSocket(OSError("socket failed"))
        processor = asyncio.create_task(connection._process_message())
        with pytest.raises(OSError, match="socket failed"):
            await connection.send_websocket_command("switch-uuid", "on")
        connection._shutdown_event.set()
        with pytest.raises(OSError, match="socket failed"):
            await processor

    asyncio.run(successful_send())
    asyncio.run(failed_send())


def test_cancelled_command_is_not_sent_later() -> None:
    class FakeWebSocket:
        state = SimpleNamespace(name="OPEN")

        def __init__(self) -> None:
            self.sent = []

        async def send(self, value) -> None:
            self.sent.append(value)

    async def scenario() -> None:
        connection = _connection()
        websocket = FakeWebSocket()
        connection.connection = websocket
        command = asyncio.create_task(
            connection.send_websocket_command("switch-uuid", "on")
        )
        await asyncio.sleep(0)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command

        connection._shutdown_event.set()
        await connection._process_message()
        assert websocket.sent == []

    asyncio.run(scenario())


def test_ventilation_turn_on_sends_requested_speed_and_preset_once() -> None:
    async def scenario() -> None:
        fan = object.__new__(LoxoneVentilation)
        fan.uuidAction = "ventilation-uuid"
        fan._stateAttribUuids = {"speed": "speed-state", "mode": "mode-state"}
        fan._stateAttribValues = {"speed-state": 0, "mode-state": 5}
        fan._mode_id_to_name = {4: "High", 5: "Auto"}
        fan._mode_name_to_id = {"High": 4, "Auto": 5}
        sent = []

        async def send(uuid, command) -> None:
            sent.append((uuid, command))

        fan.async_send_command = send
        await fan.async_turn_on(percentage=65, preset_mode="High")

        assert sent == [
            ("ventilation-uuid", "setTimer/3600/65/4/-1")
        ]

    asyncio.run(scenario())


def test_ac_control_rejects_bad_json_and_off_does_not_set_a_mode() -> None:
    assert _json_object_list("not-json") == []
    assert _json_object_list('[{"id": 2, "name": "Cool"}]') == [
        {"id": 2, "name": "Cool"}
    ]

    async def scenario() -> None:
        climate = object.__new__(LoxoneAcControl)
        climate.uuidAction = "ac-uuid"
        climate.details = {"format": "%.1f °F"}
        sent = []

        async def send(uuid, command) -> None:
            sent.append((uuid, command))

        climate.async_send_command = send
        assert climate.temperature_unit == UnitOfTemperature.FAHRENHEIT
        await climate.async_set_hvac_mode(HVACMode.OFF)
        assert sent == [("ac-uuid", "off")]

    asyncio.run(scenario())


def test_coordinator_reuses_the_websocket_opened_during_setup(monkeypatch) -> None:
    opened_socket = object()

    class FakeApi:
        def __init__(self, **_kwargs) -> None:
            self.connection = None
            self.structure_file = {"controls": {}, "msInfo": {}}
            self.open_calls = 0

        async def open(self, _session):
            self.open_calls += 1
            return opened_socket

    monkeypatch.setattr(coordinator_module, "LoxoneConnection", FakeApi)
    monkeypatch.setattr(
        coordinator_module, "async_get_clientsession", lambda _hass: object()
    )

    coordinator = object.__new__(LoxoneCoordinator)
    coordinator.hass = SimpleNamespace()
    coordinator.config_entry = SimpleNamespace(data={}, options={})
    coordinator._host = "miniserver.local"
    coordinator._port = 8080
    coordinator._username = "user"
    coordinator._password = "secret"
    coordinator._verify_ssl = True
    coordinator._allow_insecure_http = True
    coordinator.api = None
    coordinator.miniserver = None
    coordinator.action_uuids = set()

    asyncio.run(coordinator.async_config_entry_first_refresh())

    assert coordinator.api.open_calls == 1
    assert coordinator.api.connection is opened_socket


def test_protocol_text_frames_never_reach_home_assistant_callbacks() -> None:
    """Authentication tokens must remain inside the protocol layer."""
    marker = "super-secret-token-marker"
    payload = json.dumps(
        {
            "LL": {
                "control": "jdev/sps/io/example/on",
                "code": "200",
                "value": marker,
            }
        }
    ).encode()
    header = struct.pack(
        "<cBccI", b"\x03", MessageType.TEXT, b"\x00", b"\x00", len(payload)
    )

    class State:
        CLOSED = "closed"

        def __eq__(self, other) -> bool:
            return False

    class FakeConnection:
        state = State()

        def __aiter__(self):
            async def frames():
                yield header
                yield payload

            return frames()

    async def scenario() -> list[dict]:
        connection = _connection()
        callbacks = []

        async def callback(message) -> None:
            callbacks.append(message)

        await connection._do_start_listening(callback, FakeConnection())
        return callbacks

    assert MessageType.TEXT not in EXTERNAL_CALLBACK_TYPES
    assert MessageType.KEEPALIVE not in EXTERNAL_CALLBACK_TYPES
    assert asyncio.run(scenario()) == []


def test_websocket_debug_logs_never_contain_frame_payloads(
    monkeypatch, caplog
) -> None:
    marker = "secret-frame-marker"

    async def fake_recv(_self, _decode=False):
        return marker

    async def fake_send(_self, _message, _text=None):
        return None

    monkeypatch.setattr(ClientConnection, "recv", fake_recv)
    monkeypatch.setattr(ClientConnection, "send", fake_send)
    client = object.__new__(LoxoneClientConnection)
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        assert await client.recv() == marker
        await client.send([marker])

    asyncio.run(scenario())
    assert marker not in caplog.text


def test_http_timeout_is_configurable_and_positive() -> None:
    class FakeSession:
        closed = False

        def __init__(self) -> None:
            self.timeout = None

        async def get(self, _url, **kwargs):
            self.timeout = kwargs["timeout"].total
            return SimpleNamespace(status=200)

    session = FakeSession()
    client = LoxoneAsyncHttpClient(
        "miniserver.local",
        "user",
        "secret",
        session=session,
        timeout=2.5,
    )
    asyncio.run(client.get("/status"))
    assert session.timeout == 2.5

    with pytest.raises(ValueError, match="positive"):
        LoxoneAsyncHttpClient(
            "miniserver.local",
            "user",
            "secret",
            session=session,
            timeout=0,
        )


def test_http_errors_do_not_expose_hosts_or_response_bodies(caplog) -> None:
    host_marker = "private-miniserver.example"
    response_marker = "private-response-marker"

    class FailingSession:
        closed = False

        async def get(self, _url, **_kwargs):
            raise aiohttp.ClientConnectionError(response_marker)

    class Content:
        async def read(self):
            return response_marker.encode()

    client = LoxoneAsyncHttpClient(
        host_marker,
        "user",
        "secret",
        session=FailingSession(),
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConnectionError) as exc_info:
        asyncio.run(client.get("/status"))
    with pytest.raises(PermissionError) as response_exc:
        asyncio.run(
            LoxoneAsyncHttpClient._handle_error(
                SimpleNamespace(status=403, content=Content())
            )
        )

    visible = caplog.text + str(exc_info.value) + str(response_exc.value)
    assert host_marker not in visible
    assert response_marker not in visible


def test_connection_attempt_is_not_retried_inside_the_integration(monkeypatch) -> None:
    calls = 0

    class FailingHttpClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def get(self, _endpoint):
            nonlocal calls
            calls += 1
            raise ConnectionError("offline")

    monkeypatch.setattr(connection_module, "LoxoneAsyncHttpClient", FailingHttpClient)
    with pytest.raises(ConnectionError, match="offline"):
        asyncio.run(_connection(timeout=0.1).open(object()))
    assert calls == 1


def test_token_refresh_requires_a_bounded_acknowledgement() -> None:
    async def success() -> None:
        connection = _connection(timeout=0.1)
        connection._token.token = "stored-token"
        connection._key = "00" * 32
        connection._hash_alg = "SHA256"
        connection.miniserver_version = [14]
        task = asyncio.create_task(connection._refresh_token())
        await asyncio.sleep(0)
        assert connection._message_queue.qsize() == 1
        connection._token_refresh_event.set()
        await task

    async def timeout() -> None:
        connection = _connection(timeout=0.01)
        connection._token.token = "stored-token"
        connection._key = "00" * 32
        connection._hash_alg = "SHA256"
        connection.miniserver_version = [14]
        with pytest.raises(LoxoneTokenError, match="Timed out"):
            await connection._refresh_token()

    asyncio.run(success())
    asyncio.run(timeout())


def test_invalid_token_response_stops_the_connection() -> None:
    message = TextMessage(
        json.dumps(
            {
                "LL": {
                    "control": "jdev/sys/getjwt",
                    "code": "200",
                    "value": {"token": "", "validUntil": 0},
                }
            }
        )
    )
    with pytest.raises(LoxoneTokenError, match="Invalid token response"):
        asyncio.run(_connection()._websocket_event(message))


def test_diagnostics_are_entry_scoped_and_privacy_preserving() -> None:
    selected_options = {
        "selected_entities": ["secret-selected-uuid"],
        "door_profiles": {"secret-door-uuid": {"unlock_action": "secret-action"}},
        "verify_ssl": True,
        "allow_insecure_http": False,
    }
    entry = SimpleNamespace(entry_id="entry-b", options=selected_options)
    coordinator_a = SimpleNamespace(
        api=SimpleNamespace(
            is_connected=True,
            scheme="https",
            structure_file={"controls": {"secret-from-entry-a": {}}},
        ),
        miniserver=SimpleNamespace(miniserver_type=1, software_version="1.0"),
    )
    coordinator_b = SimpleNamespace(
        api=SimpleNamespace(
            is_connected=True,
            scheme="https",
            structure_file={
                "controls": {
                    "secret-control-uuid": {
                        "name": "Secret sauna name",
                        "type": "Sauna",
                        "uuidAction": "secret-action-uuid",
                    }
                },
                "rooms": {"secret-room-uuid": {"name": "Secret room"}},
                "cats": {"secret-category-uuid": {"name": "Secret category"}},
                "msInfo": {
                    "serialNr": "secret-serial",
                    "msName": "Secret home name",
                },
            },
        ),
        miniserver=SimpleNamespace(miniserver_type=2, software_version="14.1"),
    )
    hass = SimpleNamespace(
        data={"loxone": {"entry-a": coordinator_a, "entry-b": coordinator_b}}
    )

    diagnostics = asyncio.run(async_get_config_entry_diagnostics(hass, entry))
    serialized = json.dumps(diagnostics)
    assert diagnostics["selection"] == {
        "entity_count": 1,
        "door_profile_count": 1,
    }
    assert diagnostics["structure"]["control_types"] == {"Sauna": 1}
    for secret in (
        "secret-from-entry-a",
        "secret-control-uuid",
        "Secret sauna name",
        "secret-room-uuid",
        "Secret home name",
        "secret-serial",
        "secret-selected-uuid",
    ):
        assert secret not in serialized


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("https://miniserver.example:8443/base/", 8080, "https://miniserver.example:8443/base"),
        ("miniserver.local", 443, "https://miniserver.local"),
        ("miniserver.local", 8080, "http://miniserver.local:8080"),
    ],
)
def test_configuration_url_uses_the_configured_transport(
    host: str, port: int, expected: str
) -> None:
    assert _configuration_url(host, port) == expected


def test_reauthentication_replaces_credentials_and_token(monkeypatch) -> None:
    entry = SimpleNamespace(
        options={
            "host": "miniserver.local",
            "port": 8080,
            "username": "old-user",
            "password": "old-password",
            "allow_insecure_http": True,
            "selected_entities": ["selected-entity"],
        },
        unique_id="serial-1",
    )
    captured = {}

    async def read_structure(_hass, options, token=None):
        captured["validated_options"] = dict(options)
        assert token is None
        return {"msInfo": {"serialNr": "serial-1"}}, {"token": "new-token"}

    flow = LoxoneFlowHandler()
    flow.hass = object()
    monkeypatch.setattr(config_flow_module, "_async_read_structure", read_structure)
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)

    async def set_unique_id(unique_id):
        captured["unique_id"] = unique_id

    monkeypatch.setattr(flow, "async_set_unique_id", set_unique_id)
    monkeypatch.setattr(
        flow,
        "_abort_if_unique_id_mismatch",
        lambda **kwargs: captured.update(mismatch_check=kwargs),
    )

    def update_and_abort(updated_entry, **kwargs):
        captured["entry"] = updated_entry
        captured.update(kwargs)
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(flow, "async_update_reload_and_abort", update_and_abort)
    result = asyncio.run(
        flow.async_step_reauth_confirm(
            {"username": "new-user", "password": "new-password"}
        )
    )

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert captured["unique_id"] == "serial-1"
    assert captured["data"] == {"token": "new-token"}
    assert captured["options"]["username"] == "new-user"
    assert captured["options"]["password"] == "new-password"
    assert captured["options"]["selected_entities"] == ["selected-entity"]


def test_system_health_omits_private_miniserver_identifiers() -> None:
    coordinator = SimpleNamespace(
        api=SimpleNamespace(is_connected=True),
        miniserver=SimpleNamespace(
            serial="secret-serial",
            name="Secret project",
            software_version="14.1",
            miniserver_type=2,
            lox_config=SimpleNamespace(
                json={
                    "msInfo": {
                        "localUrl": "secret.local",
                        "remoteUrl": "https://secret.example",
                    }
                }
            ),
        ),
    )
    hass = SimpleNamespace(data={"loxone": {"entry-id": coordinator}})
    health = asyncio.run(system_health_info(hass))
    serialized = json.dumps(health)

    assert health["Configured Miniservers"] == 1
    assert health["Connected Miniservers"] == 1
    assert health["Loxone Software Versions"] == ["14.1"]
    for secret in (
        "secret-serial",
        "Secret project",
        "secret.local",
        "secret.example",
    ):
        assert secret not in serialized
