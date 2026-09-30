"""Synthetic lock feedback only: no real endpoints, credentials or actuation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

import custom_components.loxone.lock as lock_module
from custom_components.loxone.catalog import (
    access_lock_selection_key, build_entity_catalog, build_feedback_options,
    entity_is_selected,
)
from custom_components.loxone.const import CONF_DOOR_PROFILES, CONF_SELECTED_ENTITIES
from custom_components.loxone.lock import LoxoneDoorLock, parse_window_states
from custom_components.loxone.lock_state import binary_feedback


def _entity(*, binary=False, profile=None, secured=False):
    api = SimpleNamespace(connection=object(), is_connected=True)
    entity = LoxoneDoorLock(
        hass=SimpleNamespace(), monitor_uuid="synthetic-control", state_uuid="status",
        index=0, door_id="synthetic-door", name="Test door", room="Test room",
        profile=profile or {}, allowed_actions={"actuator|pulse"},
        secured_actions={"actuator|pulse"} if secured else set(),
        config_entry_id="entry-one", gateway_id="gateway-one", binary_source=binary,
    )
    entity._get_coordinator = lambda: SimpleNamespace(api=api)
    entity.async_write_ha_state = Mock()
    entity.async_send_command = AsyncMock()
    entity.async_send_secured_command = AsyncMock()
    return entity, api


def _event(entity, value, entry="entry-one"):
    asyncio.run(entity.event_handler(SimpleNamespace(data={"config_entry_id": entry, "status": value})))


@pytest.mark.parametrize("source,expected", [(1, True), (0, False), ("1", True), ("0", False), ("true", True), ("off", False)])
def test_binary_feedback_is_exact(source, expected):
    assert binary_feedback(source) is expected
    assert binary_feedback(source, inverted=True) is (not expected)


@pytest.mark.parametrize("value", [None, "unknown", "", 2, -1, 0.5, float("nan"), float("inf"), {}, []])
def test_invalid_binary_feedback_never_becomes_locked(value):
    assert binary_feedback(value) is None


@pytest.mark.parametrize("value,locked,opened", [("8", True, False), ("9", True, False), ("16", False, False), ("17", False, False), ("4", False, True), ("1", None, False), ("0", None, None)])
def test_window_monitor_status_is_authoritative(value, locked, opened):
    entity, _ = _entity()
    _event(entity, value)
    assert entity.is_locked is locked
    assert entity.is_open is opened


def test_invalid_window_values_are_not_truncated():
    assert parse_window_states("8.5,32,NaN,Infinity,-8") == [0, 0, 0, 0, 0]


def test_missing_window_clears_previous_locked_state():
    entity, _ = _entity()
    _event(entity, "8")
    _event(entity, [])
    assert entity.is_locked is None
    assert entity.extra_state_attributes["raw_window_state"] is None


def test_direct_digital_state_and_invalid_feedback():
    entity, _ = _entity(binary=True)
    assert entity.available and entity.is_locked is None
    _event(entity, 1)
    assert entity.is_locked is True
    _event(entity, 0)
    assert entity.is_locked is False
    assert entity.is_open is None  # Unlocked does not mean latch released.
    _event(entity, "unknown")
    assert entity.is_locked is None


def test_explicit_reverse_polarity():
    entity, _ = _entity(binary=True, profile={"invert_locked_state": True})
    _event(entity, 0)
    assert entity.is_locked is True


@pytest.mark.parametrize("entry", ["entry-two", None])
def test_foreign_or_unscoped_events_do_not_change_state(entry):
    entity, _ = _entity(binary=True)
    _event(entity, 1, entry)
    assert entity.is_locked is None


def test_disconnect_and_reconnect_require_fresh_feedback():
    entity, api = _entity(binary=True)
    _event(entity, 1)
    api.is_connected = False
    assert not entity.available
    assert entity.is_locked is None
    api.connection = object()
    api.is_connected = True
    assert entity.available and entity.is_locked is None
    _event(entity, 0)
    assert entity.is_locked is False


def test_open_request_never_changes_the_reported_lock_state():
    entity, _ = _entity(binary=True, profile={"open_action": "actuator|pulse"})
    _event(entity, 1)
    asyncio.run(entity.async_open())
    entity.async_send_command.assert_awaited_once_with("actuator", "pulse")
    assert entity.is_locked is True
    _event(entity, 0)
    assert entity.is_locked is False


def test_secured_action_fails_closed_and_never_falls_back():
    entity, _ = _entity(profile={"open_action": "actuator|pulse"}, secured=True)
    with pytest.raises(HomeAssistantError, match="visualization"):
        asyncio.run(entity.async_open())
    entity.async_send_command.assert_not_awaited()
    entity.async_send_secured_command.assert_not_awaited()
    asyncio.run(entity.async_open(code="synthetic-test-code"))
    entity.async_send_secured_command.assert_awaited_once_with("actuator", "pulse", "synthetic-test-code")
    entity.async_send_command.assert_not_awaited()
    assert "synthetic-test-code" not in str(entity.extra_state_attributes)
    assert entity.is_locked is None


@pytest.mark.parametrize("profile", [{}, {"open_action": "deleted|pulse"}])
def test_unmapped_or_deleted_action_is_blocked(profile):
    entity, _ = _entity(profile=profile)
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.async_open())
    entity.async_send_command.assert_not_awaited()


def _structure():
    return {"controls": {
        "reader": {"uuidAction": "reader", "type": "NfcCodeTouch", "name": "Reader", "isSecured": True,
                   "states": {"deviceState": "device", "jLocked": "inhibited"},
                   "details": {"accessOutputs": {"q1": "Door", "q2": "Side door"}}},
        "release": {"uuidAction": "release", "type": "Pushbutton", "name": "Release",
                    "states": {"active": "pulse-status", "lockedOn": "forced"}},
        "feedback": {"uuidAction": "feedback", "type": "InfoOnlyDigital", "name": "Lock status", "states": {"active": "bolt"}},
    }}


def test_access_locks_are_separate_opt_in_candidates():
    keys = {c.key for c in build_entity_catalog(_structure())}
    assert "action:reader:output/1" in keys
    for control, action in (("reader", "output/1"), ("reader", "output/2"), ("release", "pulse")):
        key = access_lock_selection_key(control, action)
        assert key in keys
        assert not entity_is_selected(SimpleNamespace(options={}), key)
    assert {x["value"] for x in build_feedback_options(_structure())} == {"bolt"}


def test_setup_rejects_control_flags_as_lock_feedback(monkeypatch):
    structure = _structure()
    monkeypatch.setattr(lock_module, "get_miniserver_from_hass", lambda *_: SimpleNamespace(lox_config=SimpleNamespace(json=structure)))
    key = access_lock_selection_key("reader", "output/1")
    entry = SimpleNamespace(entry_id="test-entry", unique_id="test-gateway", options={
        CONF_SELECTED_ENTITIES: [key], CONF_DOOR_PROFILES: {key: {"locked_state": "inhibited", "open_action": "reader|output/1"}},
    })
    entities = []
    asyncio.run(lock_module.async_setup_entry(SimpleNamespace(), entry, entities.extend))
    assert len(entities) == 1
    entity = entities[0]
    assert entity._state_uuid == ""
    assert entity.code_format == ".+"
    assert entity.unique_id == "test-gateway-reader-access-lock-output/1"


def test_native_lock_identity_is_unchanged():
    entity, _ = _entity()
    assert entity.unique_id == "gateway-one-synthetic-control-door-synthetic-door"


def test_profile_flow_exposes_and_stores_direct_feedback():
    from custom_components.loxone.config_flow import _EntitySelectionMixin

    class Flow(_EntitySelectionMixin):
        def async_show_form(self, **kwargs):
            return kwargs

        async def _async_finish_selection(self):
            return self._door_profiles

    flow = Flow()
    flow._structure = _structure()
    flow._candidates = build_entity_catalog(flow._structure)
    key = access_lock_selection_key("reader", "output/1")
    flow._door_keys = [key]
    flow._door_profiles = {}
    flow._door_position = 0
    form = asyncio.run(flow.async_step_door_profile())
    fields = {str(key) for key in form["data_schema"].schema}
    assert {"locked_state", "invert_locked_state", "assume_closed_after_open"} <= fields
    result = asyncio.run(flow.async_step_door_profile({"locked_state": "bolt", "assume_closed_after_open": True}))
    assert result[key]["locked_state"] == "bolt"
    assert result[key]["invert_locked_state"] is False
    assert result[key]["open_action"] is None
    assert result[key]["assume_closed_after_open"] is True


def test_local_transport_timer_clears_state_and_is_removed(monkeypatch):
    entity, api = _entity(binary=True)
    entity.hass.bus = SimpleNamespace(async_listen=Mock(return_value=Mock()))
    entity.async_on_remove = Mock()
    unsubscribe = Mock()
    timer = {}

    def register(hass, callback, interval):
        timer["callback"] = callback
        assert interval.total_seconds() == 5
        return unsubscribe

    monkeypatch.setattr(lock_module, "async_track_time_interval", register)
    asyncio.run(entity.async_added_to_hass())
    entity.async_on_remove.assert_called_once_with(unsubscribe)
    _event(entity, 1)
    entity.async_write_ha_state.reset_mock()
    asyncio.run(timer["callback"](None))
    entity.async_write_ha_state.assert_not_called()
    api.is_connected = False
    asyncio.run(timer["callback"](None))
    assert entity._reported_locked is None
    assert not entity.available
    api.is_connected = True
    api.connection = object()
    asyncio.run(timer["callback"](None))
    assert entity.available and entity.is_locked is None
    assert entity.async_write_ha_state.call_count == 2


def _fallback(monkeypatch, **kwargs):
    entity, api = _entity(profile={"open_action": "actuator|pulse", "assume_closed_after_open": True}, **kwargs)
    timers = []

    def register(hass, delay, callback):
        cancel = Mock()
        timers.append((delay, callback, cancel))
        return cancel

    monkeypatch.setattr(lock_module, "async_call_later", register)
    return entity, api, timers


@pytest.mark.parametrize("binary", [True, False])
def test_opt_in_fallback_changes_only_display_after_five_seconds(monkeypatch, binary):
    entity, _, timers = _fallback(monkeypatch, binary=binary)
    assert entity.is_locked is None and not entity.assumed_state
    asyncio.run(entity.async_open())
    assert len(timers) == 1 and timers[0][0] == 5
    assert entity.is_locked is False and entity.is_open is True
    assert entity.assumed_state and entity.icon == "mdi:lock-question"
    asyncio.run(timers[0][1](None))
    assert entity.is_locked is True and entity.is_open is False
    assert entity.extra_state_attributes["state_estimated"] is True
    assert entity.extra_state_attributes["state_basis"] == "five_second_open_fallback"
    entity.async_send_command.assert_awaited_once_with("actuator", "pulse")


def test_default_no_feedback_does_not_start_an_estimate(monkeypatch):
    schedule = Mock()
    monkeypatch.setattr(lock_module, "async_call_later", schedule)
    entity, _ = _entity(binary=True, profile={"open_action": "actuator|pulse"})
    asyncio.run(entity.async_open())
    schedule.assert_not_called()
    assert entity.is_locked is None and not entity.assumed_state


@pytest.mark.parametrize("value", [0, 1])
def test_existing_real_feedback_prevents_fallback(monkeypatch, value):
    entity, _, timers = _fallback(monkeypatch, binary=True)
    _event(entity, value)
    asyncio.run(entity.async_open())
    assert not timers
    assert entity.is_locked is bool(value) and not entity.assumed_state


@pytest.mark.parametrize("value", [0, 1, "unknown"])
def test_feedback_cancels_fallback_even_if_callback_was_queued(monkeypatch, value):
    entity, _, timers = _fallback(monkeypatch, binary=True)
    asyncio.run(entity.async_open())
    _event(entity, value)
    timers[0][2].assert_called_once()
    asyncio.run(timers[0][1](None))
    assert entity.is_locked is binary_feedback(value)
    assert not entity.assumed_state


def test_repeated_open_replaces_timer(monkeypatch):
    entity, _, timers = _fallback(monkeypatch, binary=True)
    asyncio.run(entity.async_open())
    asyncio.run(entity.async_open())
    timers[0][2].assert_called_once()
    assert len(timers) == 2
    asyncio.run(timers[0][1](None))
    assert entity.is_locked is False
    asyncio.run(timers[1][1](None))
    assert entity.is_locked is True


def test_failed_repeat_clears_old_estimate_without_starting_timer(monkeypatch):
    entity, _, timers = _fallback(monkeypatch, binary=True)
    asyncio.run(entity.async_open())
    entity.async_send_command.side_effect = HomeAssistantError("synthetic failure")
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.async_open())
    assert len(timers) == 1
    asyncio.run(timers[0][1](None))
    assert entity.is_locked is None and not entity.assumed_state


def test_failed_secured_request_does_not_start_fallback(monkeypatch):
    entity, _, timers = _fallback(monkeypatch, binary=True, secured=True)
    with pytest.raises(HomeAssistantError):
        asyncio.run(entity.async_open())
    assert not timers and entity.is_locked is None


@pytest.mark.parametrize("change", ["disconnect", "new_connection", "unload"])
def test_transport_or_unload_never_leaves_assumed_locked(monkeypatch, change):
    entity, api, timers = _fallback(monkeypatch, binary=True)
    asyncio.run(entity.async_open())
    if change == "disconnect":
        api.is_connected = False
    elif change == "new_connection":
        api.connection = object()
    else:
        asyncio.run(entity.async_will_remove_from_hass())
        timers[0][2].assert_called_once()
    asyncio.run(timers[0][1](None))
    assert entity.is_locked is None and not entity.assumed_state


def test_feedback_during_send_is_not_overridden(monkeypatch):
    entity, _, timers = _fallback(monkeypatch, binary=True)

    async def send(*_):
        await entity.event_handler(SimpleNamespace(data={"config_entry_id": "entry-one", "status": 0}))

    entity.async_send_command.side_effect = send
    asyncio.run(entity.async_open())
    assert not timers and entity.is_locked is False and not entity.assumed_state


@pytest.mark.parametrize("operation", ["async_lock", "async_unlock"])
def test_other_command_cancels_open_fallback(monkeypatch, operation):
    entity, _, timers = _fallback(monkeypatch, binary=True)
    asyncio.run(entity.async_open())
    # Even a rejected/unmapped command invalidates an old display estimate.
    with pytest.raises(HomeAssistantError):
        asyncio.run(getattr(entity, operation)())
    asyncio.run(timers[0][1](None))
    assert entity.is_locked is None


def test_older_inflight_open_cannot_replace_newer_timer(monkeypatch):
    entity, _, timers = _fallback(monkeypatch, binary=True)

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        count = 0

        async def send(*_):
            nonlocal count
            count += 1
            if count == 1:
                started.set()
                await release.wait()

        entity.async_send_command.side_effect = send
        first = asyncio.create_task(entity.async_open())
        await started.wait()
        await entity.async_open()
        assert len(timers) == 1
        release.set()
        await first
        assert len(timers) == 1
        await timers[0][1](None)
        assert entity.is_locked is True

    asyncio.run(run())
