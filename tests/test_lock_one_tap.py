"""Synthetic one-tap authorization; never connect to or actuate a real door."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from test_lock_feedback import _entity
import custom_components.loxone.lock as lock_module


def _one_tap(user_id="admin", user=None, password="synthetic-login-only"):
    entity, api = _entity(binary=True, secured=True, profile={
        "open_action": "actuator|pulse", "lock_action": "actuator|pulse",
        "unlock_action": "actuator|pulse", "admin_one_tap_use_login_password": True,
    })
    entity.entity_id = "lock.synthetic"
    entity.async_set_context(Context(user_id=user_id))
    user = user if user is not None else SimpleNamespace(is_active=True, is_admin=True)
    entry = SimpleNamespace(domain="loxone", options={"password": password})
    entity.hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=user))
    entity.hass.config_entries = SimpleNamespace(async_get_entry=Mock(return_value=entry))
    return entity, api, entry


@pytest.mark.parametrize("method", ["async_open", "async_lock", "async_unlock"])
def test_admin_can_use_existing_owning_entry_password(method):
    entity, _, entry = _one_tap()
    assert entity.code_format is None
    asyncio.run(getattr(entity, method)())
    entity.hass.config_entries.async_get_entry.assert_called_once_with("entry-one")
    entity.async_send_secured_command.assert_awaited_once_with(
        "actuator", "pulse", entry.options["password"]
    )
    entity.async_send_command.assert_not_awaited()
    assert entry.options["password"] not in json.dumps(entity.extra_state_attributes)
    assert "password" not in entity._profile


@pytest.mark.parametrize("method", ["async_open", "async_lock", "async_unlock"])
@pytest.mark.parametrize("case", ["anonymous", "unknown", "inactive", "non_admin"])
def test_denied_context_cannot_send_or_change_estimate(method, case):
    entity, _, _ = _one_tap(user_id=None if case == "anonymous" else "user")
    if case == "unknown":
        entity.hass.auth.async_get_user.return_value = None
    else:
        entity.hass.auth.async_get_user.return_value = SimpleNamespace(
            is_active=case != "inactive", is_admin=case != "non_admin"
        )
    with pytest.raises(Unauthorized):
        asyncio.run(getattr(entity, method)(code="supplied-code-does-not-bypass"))
    entity.async_send_command.assert_not_awaited()
    entity.async_send_secured_command.assert_not_awaited()
    entity.hass.config_entries.async_get_entry.assert_not_called()
    entity.async_write_ha_state.assert_not_called()
    assert not entity.assumed_state


@pytest.mark.parametrize("password", [None, "", False, 12])
def test_missing_password_fails_without_insecure_fallback(password):
    entity, _, _ = _one_tap(password=password)
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.async_open())
    entity.async_send_secured_command.assert_not_awaited()
    entity.async_send_command.assert_not_awaited()


@pytest.mark.parametrize("entry", [None, SimpleNamespace(domain="other", options={})])
def test_missing_or_wrong_entry_fails_closed(entry):
    entity, _, _ = _one_tap()
    entity.hass.config_entries.async_get_entry.return_value = entry
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.async_open())
    entity.async_send_secured_command.assert_not_awaited()


def test_disabled_option_never_uses_stored_password():
    entity, _, _ = _one_tap()
    entity._admin_one_tap = False
    assert entity.code_format == ".+"
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.async_open())
    entity.hass.auth.async_get_user.assert_not_awaited()
    entity.hass.config_entries.async_get_entry.assert_not_called()
    entity.async_send_secured_command.assert_not_awaited()


def test_credential_changes_are_used_without_an_extra_copy():
    entity, _, entry = _one_tap()
    entry.options["password"] = "rotated-synthetic-password"
    asyncio.run(entity.async_open())
    assert entity.async_send_secured_command.call_args.args[2] == entry.options["password"]


def test_transport_errors_do_not_expose_reused_password():
    entity, _, entry = _one_tap()
    entity.async_send_secured_command.side_effect = RuntimeError(entry.options["password"])
    with pytest.raises(HomeAssistantError) as exc:
        asyncio.run(entity.async_open())
    assert entry.options["password"] not in str(exc.value)
    assert exc.value.__suppress_context__
    assert not entity.assumed_state


def test_admin_authorization_uses_captured_not_later_context():
    entity, _, _ = _one_tap(user_id="non-admin")
    async def lookup(user_id):
        assert user_id == "non-admin"
        entity.async_set_context(Context(user_id="admin"))
        return SimpleNamespace(is_active=True, is_admin=False)
    entity.hass.auth.async_get_user.side_effect = lookup
    with pytest.raises(Unauthorized):
        asyncio.run(entity.async_open())
    entity.async_send_secured_command.assert_not_awaited()


def test_admin_open_works_with_five_second_estimate(monkeypatch):
    entity, _, _ = _one_tap()
    entity._assume_closed = True
    timer = {}
    def schedule(hass, delay, callback):
        assert delay == 5
        timer["callback"] = callback
        return Mock()
    monkeypatch.setattr(lock_module, "async_call_later", schedule)
    asyncio.run(entity.async_open())
    assert entity.is_locked is False and entity.assumed_state
    asyncio.run(timer["callback"](None))
    assert entity.is_locked is True and entity.assumed_state
    assert entity.async_send_secured_command.await_count == 1
