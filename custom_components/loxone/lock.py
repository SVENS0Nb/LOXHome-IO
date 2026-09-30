"""Loxone lock feedback with an explicit, labelled five-second fallback."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import LoxoneEntity
from .catalog import (
    access_lock_selection_key,
    action_is_secured,
    build_action_options,
    build_feedback_options,
    decode_action,
    door_identities,
    door_selection_key,
    entity_is_selected,
)
from .const import CONF_DOOR_PROFILES
from .helpers import get_all, get_or_create_device, get_room_name_from_room_uuid
from .miniserver import get_miniserver_from_hass
from .lock_state import binary_feedback, window_state

WINDOW_CLOSED = 1
WINDOW_TILTED = 2
WINDOW_OPEN = 4
WINDOW_LOCKED = 8
WINDOW_UNLOCKED = 16


def parse_window_states(value: Any) -> list[int]:
    """Parse the WindowMonitor state list defensively."""
    if isinstance(value, (list, tuple)):
        return [window_state(item) for item in value]
    if not isinstance(value, str):
        return []
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(decoded, list):
                return parse_window_states(decoded)
    return [window_state(item.strip()) for item in re.split(r"[,;|]", stripped)]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create explicitly selected native doors and access-output profiles."""
    miniserver = get_miniserver_from_hass(hass, config_entry)
    structure = miniserver.lox_config.json
    configured_profiles = config_entry.options.get(CONF_DOOR_PROFILES, {}) or {}
    profiles = configured_profiles if isinstance(configured_profiles, dict) else {}
    allowed_actions = {
        option["value"] for option in build_action_options(structure)
    }
    secured_actions = {
        action for action in allowed_actions
        if action_is_secured(structure, decode_action(action)[0])
    }
    entities: list[LoxoneDoorLock] = []

    for monitor in get_all(structure, "WindowMonitor"):
        monitor_uuid = str(monitor["uuidAction"])
        windows = monitor.get("details", {}).get("windows", ()) or ()
        for index, (item, door_id) in enumerate(
            zip(windows, door_identities(windows), strict=False)
        ):
            window = item if isinstance(item, dict) else {"name": str(item)}
            key = door_selection_key(monitor_uuid, door_id)
            legacy_key = door_selection_key(monitor_uuid, index)
            if not entity_is_selected(config_entry, key, legacy_key):
                continue
            room = get_room_name_from_room_uuid(structure, str(window.get("room") or monitor.get("room", "")))
            profile = profiles.get(key, profiles.get(legacy_key, {}))
            if not isinstance(profile, dict):
                profile = {}
            entities.append(
                LoxoneDoorLock(
                    hass=hass,
                    monitor_uuid=monitor_uuid,
                    state_uuid=monitor.get("states", {}).get("windowStates", ""),
                    index=index,
                    door_id=door_id,
                    name=window.get("name") or f"{monitor.get('name', 'Tür')} {index + 1}",
                    room=room,
                    profile=profile,
                    allowed_actions=allowed_actions,
                    secured_actions=secured_actions,
                    config_entry_id=config_entry.entry_id,
                    gateway_id=config_entry.unique_id or config_entry.entry_id,
                )
            )

    feedback_states = {option["value"] for option in build_feedback_options(structure)}
    for fallback, control in (structure.get("controls", {}) or {}).items():
        control_type = control.get("type")
        uuid = str(control.get("uuidAction") or fallback)
        name = str(control.get("name") or control_type)
        outputs = {}
        if control_type == "Pushbutton":
            outputs["pulse"] = name
        elif control_type in {"NfcCodeTouch", "NFCCodeTouch", "NFC Code Touch"}:
            outputs = {
                f"output/{str(key).lower().removeprefix('q')}": f"{name} · {label}"
                for key, label in (control.get("details", {}).get("accessOutputs", {}) or {}).items()
            }
        for command, label in outputs.items():
            key = access_lock_selection_key(uuid, command)
            if not entity_is_selected(config_entry, key):
                continue
            profile = profiles.get(key, {})
            if not isinstance(profile, dict):
                profile = {}
            state_uuid = profile.get("locked_state")
            # Do not accept arbitrary UUIDs, jLocked, lockedOn or event history.
            if state_uuid not in feedback_states:
                state_uuid = ""
            entities.append(LoxoneDoorLock(
                hass=hass, monitor_uuid=uuid, state_uuid=state_uuid,
                index=0, door_id=command, name=label,
                room=get_room_name_from_room_uuid(structure, control.get("room", "")),
                profile=profile, allowed_actions=allowed_actions,
                secured_actions=secured_actions,
                config_entry_id=config_entry.entry_id,
                gateway_id=config_entry.unique_id or config_entry.entry_id,
                binary_source=True,
            ))

    async_add_entities(entities)


class LoxoneDoorLock(LoxoneEntity, LockEntity):
    """A physical lock state with explicitly mapped actuator commands."""

    _attr_should_poll = False

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        monitor_uuid: str,
        state_uuid: str,
        index: int,
        door_id: str,
        name: str,
        room: str,
        profile: dict[str, Any],
        allowed_actions: set[str],
        config_entry_id: str,
        gateway_id: str,
        secured_actions: set[str] | None = None,
        binary_source: bool = False,
    ) -> None:
        super().__init__(
            uuidAction=monitor_uuid,
            name=name,
            room=room,
            cat="",
            type="WindowMonitor",
            config_entry_id=config_entry_id,
            gateway_id=gateway_id,
        )
        self.hass = hass
        self._monitor_uuid = monitor_uuid
        self._state_uuid = state_uuid
        self._index = index
        self._door_id = door_id
        self._profile = profile
        self._allowed_actions = allowed_actions
        self._gateway_id = gateway_id
        self._secured_actions = secured_actions or set()
        self._binary_source = binary_source
        self._reported_locked: bool | None = None
        self._feedback_connection = None
        self._window_state: int | None = None
        self._assume_closed = profile.get("assume_closed_after_open") is True
        self._assumed_locked: bool | None = None
        self._assumption_connection = None
        self._cancel_assumption = None
        self._command_generation = 0
        self._feedback_revision = 0
        self._attr_device_info = get_or_create_device(
            f"{gateway_id}-{monitor_uuid}-{door_id}",
            name,
            "Loxone Türschloss",
            room,
        )
        open_action = profile.get("open_action")
        if decode_action(open_action) and open_action in allowed_actions:
            self._attr_supported_features = LockEntityFeature.OPEN

    def _connection(self):
        """Only read in-memory transport health; no I/O from properties."""
        try:
            api = self._get_coordinator().api
        except HomeAssistantError:
            return None
        return api.connection if api and api.is_connected else None

    @property
    def available(self) -> bool:
        return self._connection() is not None

    def _has_current_feedback(self) -> bool:
        connection = self._connection()
        return connection is not None and connection is self._feedback_connection

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_connection = self._connection()

        async def check_transport(_now):
            nonlocal last_connection
            # Repaint disconnects even without a subsequent state event. This is
            # a local health check, not network polling or a synthetic door state.
            connection = self._connection()
            if connection is last_connection:
                return
            last_connection = connection
            self._clear_assumption()
            if not self._has_current_feedback():
                self._window_state = None
                self._reported_locked = None
                self._feedback_connection = None
            self.async_write_ha_state()

        self.async_on_remove(async_track_time_interval(
            self.hass, check_transport, timedelta(seconds=5)
        ))

    def _clear_assumption(self):
        self._command_generation += 1
        if self._cancel_assumption is not None:
            self._cancel_assumption()
            self._cancel_assumption = None
        self._assumed_locked = None
        self._assumption_connection = None

    async def async_will_remove_from_hass(self):
        self._clear_assumption()
        await super().async_will_remove_from_hass()

    def _current_assumption(self) -> bool | None:
        connection = self._connection()
        if connection is not None and connection is self._assumption_connection:
            return self._assumed_locked
        return None

    @property
    def unique_id(self) -> str:
        if self._binary_source:
            return f"{self._gateway_id}-{self._monitor_uuid}-access-lock-{self._door_id}"
        return f"{self._gateway_id}-{self._monitor_uuid}-door-{self._door_id}"

    def _loxone_is_locked(self) -> bool | None:
        if not self._has_current_feedback():
            return None
        if self._binary_source:
            return self._reported_locked
        if self._window_state is None:
            return None
        if self._window_state & (WINDOW_UNLOCKED | WINDOW_OPEN | WINDOW_TILTED):
            return False
        if self._window_state & WINDOW_LOCKED:
            return True
        return None

    @property
    def is_locked(self) -> bool | None:
        reported = self._loxone_is_locked()
        return reported if reported is not None else self._current_assumption()

    @property
    def assumed_state(self) -> bool:
        return self._loxone_is_locked() is None and self._current_assumption() is not None

    @property
    def icon(self) -> str | None:
        return "mdi:lock-question" if self.assumed_state else None

    @property
    def is_open(self) -> bool | None:
        if self._binary_source or not self._has_current_feedback() or self._window_state is None:
            assumed = self._current_assumption() if self._loxone_is_locked() is None else None
            return not assumed if assumed is not None else None
        return bool(self._window_state & WINDOW_OPEN)

    async def event_handler(self, event) -> None:
        if event.data.get("config_entry_id") != self._config_entry_id:
            return
        if not self._state_uuid or self._state_uuid not in event.data:
            return
        self._feedback_revision += 1
        self._clear_assumption()
        self._feedback_connection = self._connection()
        if self._binary_source:
            self._reported_locked = binary_feedback(
                event.data[self._state_uuid], self._profile.get("invert_locked_state") is True
            )
            self.async_write_ha_state()
            return
        states = parse_window_states(event.data[self._state_uuid])
        if self._index >= len(states):
            self._window_state = None
        else:
            self._window_state = states[self._index] or None
        self.async_write_ha_state()

    async def _async_execute(self, profile_key: str, code: str | None = None) -> None:
        action = decode_action(self._profile.get(profile_key))
        if action is None:
            raise HomeAssistantError(f"Für {self.name} ist keine Aktion '{profile_key}' konfiguriert")
        encoded_action = self._profile.get(profile_key)
        if encoded_action not in self._allowed_actions:
            raise HomeAssistantError(
                f"Die konfigurierte Aktion '{profile_key}' existiert nicht mehr im Loxone-Projekt"
            )
        uuid, command = action
        if encoded_action in self._secured_actions:
            if not isinstance(code, str) or not code:
                raise HomeAssistantError("This Loxone action requires its visualization password")
            await self.async_send_secured_command(uuid, command, code)
        else:
            await self.async_send_command(uuid, command)

    @property
    def code_format(self) -> str | None:
        if any(self._profile.get(key) in self._secured_actions for key in (
            "lock_action", "unlock_action", "open_action"
        )):
            return ".+"
        return None

    async def async_lock(self, **kwargs: Any) -> None:
        self._clear_assumption()
        self.async_write_ha_state()
        await self._async_execute("lock_action", kwargs.get("code"))

    async def async_unlock(self, **kwargs: Any) -> None:
        self._clear_assumption()
        self.async_write_ha_state()
        await self._async_execute("unlock_action", kwargs.get("code"))

    async def async_open(self, **kwargs: Any) -> None:
        self._clear_assumption()
        self.async_write_ha_state()
        generation = self._command_generation
        revision = self._feedback_revision
        connection = self._connection()
        await self._async_execute("open_action", kwargs.get("code"))
        # Successful transmission is NOT confirmation of physical movement.
        # This opt-in display estimate cannot supersede a real status update.
        if (not self._assume_closed or connection is None
                or self._connection() is not connection
                or self._command_generation != generation
                or self._feedback_revision != revision
                or self._loxone_is_locked() is not None):
            return
        self._assumed_locked = False
        self._assumption_connection = connection

        async def assume_closed(_now):
            if generation != self._command_generation:
                return
            self._cancel_assumption = None
            if self._connection() is connection and self._loxone_is_locked() is None:
                self._assumed_locked = True
            else:
                self._clear_assumption()
            self.async_write_ha_state()

        self._cancel_assumption = async_call_later(self.hass, 5, assume_closed)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self._attr_extra_state_attributes,
            "monitor_uuid": self._monitor_uuid,
            "monitor_index": self._index,
            "door_uuid": self._door_id,
            "raw_window_state": self._window_state if self._has_current_feedback() else None,
            "state_source": self._state_uuid or None,
            "state_source_kind": "loxone_digital" if self._binary_source else "loxone_window_monitor",
            "state_estimated": self.assumed_state,
            "state_basis": "five_second_open_fallback" if self.assumed_state else (
                "loxone_feedback" if self._loxone_is_locked() is not None else "unknown"
            ),
            "assumed_close_delay_seconds": 5 if self._assume_closed else None,
        }
