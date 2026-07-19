"""Security and connection-lifecycle regression tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature

import custom_components.loxone.coordinator as coordinator_module
from custom_components.loxone.catalog import feature_is_enabled
from custom_components.loxone.climate import LoxoneAcControl, _json_object_list
from custom_components.loxone.config_flow import _validate_transport
from custom_components.loxone.const import CONF_ALLOW_INSECURE_HTTP
from custom_components.loxone.coordinator import LoxoneCoordinator
from custom_components.loxone.fan import LoxoneVentilation
from custom_components.loxone.lights.colorpickers import _parse_color_value
from custom_components.loxone.pyloxone_api.connection import LoxoneConnection
from custom_components.loxone.sauna import _as_bool


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
