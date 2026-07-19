"""Physical door locks derived from Loxone WindowMonitor states."""

from __future__ import annotations

import json
import re
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LoxoneEntity
from .catalog import (
    build_action_options,
    decode_action,
    door_identities,
    door_selection_key,
    entity_is_selected,
)
from .const import CONF_DOOR_PROFILES
from .helpers import get_all, get_or_create_device, get_room_name_from_room_uuid
from .miniserver import get_miniserver_from_hass

WINDOW_CLOSED = 1
WINDOW_TILTED = 2
WINDOW_OPEN = 4
WINDOW_LOCKED = 8
WINDOW_UNLOCKED = 16


def parse_window_states(value: Any) -> list[int]:
    """Parse the WindowMonitor state list defensively."""
    if isinstance(value, (list, tuple)):
        result: list[int] = []
        for item in value:
            try:
                result.append(max(0, int(float(item))))
            except (TypeError, ValueError, OverflowError):
                result.append(0)
        return result
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
    result = []
    for item in re.split(r"[,;|]", stripped):
        try:
            result.append(max(0, int(float(item.strip()))))
        except (ValueError, OverflowError):
            result.append(0)
    return result


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create only explicitly selected WindowMonitor doors."""
    miniserver = get_miniserver_from_hass(hass, config_entry)
    structure = miniserver.lox_config.json
    configured_profiles = config_entry.options.get(CONF_DOOR_PROFILES, {}) or {}
    profiles = configured_profiles if isinstance(configured_profiles, dict) else {}
    allowed_actions = {
        option["value"] for option in build_action_options(structure)
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
                    config_entry_id=config_entry.entry_id,
                    gateway_id=config_entry.unique_id or config_entry.entry_id,
                )
            )

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
        profile: dict[str, str | None],
        allowed_actions: set[str],
        config_entry_id: str,
        gateway_id: str,
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
        self._window_state: int | None = None
        self._attr_available = False
        self._attr_device_info = get_or_create_device(
            f"{gateway_id}-{monitor_uuid}-{door_id}",
            name,
            "Loxone Türschloss",
            room,
        )
        open_action = profile.get("open_action")
        if decode_action(open_action) and open_action in allowed_actions:
            self._attr_supported_features = LockEntityFeature.OPEN

    @property
    def unique_id(self) -> str:
        return f"{self._gateway_id}-{self._monitor_uuid}-door-{self._door_id}"

    @property
    def is_locked(self) -> bool | None:
        if self._window_state is None:
            return None
        if self._window_state & (WINDOW_UNLOCKED | WINDOW_OPEN):
            return False
        if self._window_state & WINDOW_LOCKED:
            return True
        return None

    @property
    def is_open(self) -> bool | None:
        if self._window_state is None:
            return None
        return bool(self._window_state & WINDOW_OPEN)

    async def event_handler(self, event) -> None:
        if self._state_uuid not in event.data:
            return
        states = parse_window_states(event.data[self._state_uuid])
        if self._index >= len(states):
            self._attr_available = False
        else:
            self._window_state = states[self._index]
            self._attr_available = self._window_state != 0
        self.async_write_ha_state()

    async def _async_execute(self, profile_key: str) -> None:
        action = decode_action(self._profile.get(profile_key))
        if action is None:
            raise HomeAssistantError(f"Für {self.name} ist keine Aktion '{profile_key}' konfiguriert")
        encoded_action = self._profile.get(profile_key)
        if encoded_action not in self._allowed_actions:
            raise HomeAssistantError(
                f"Die konfigurierte Aktion '{profile_key}' existiert nicht mehr im Loxone-Projekt"
            )
        uuid, command = action
        await self.async_send_command(uuid, command)

    async def async_lock(self, **kwargs: Any) -> None:
        await self._async_execute("lock_action")

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._async_execute("unlock_action")

    async def async_open(self, **kwargs: Any) -> None:
        await self._async_execute("open_action")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self._attr_extra_state_attributes,
            "monitor_uuid": self._monitor_uuid,
            "monitor_index": self._index,
            "door_uuid": self._door_id,
            "raw_window_state": self._window_state,
        }
