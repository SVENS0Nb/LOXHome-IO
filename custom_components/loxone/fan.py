"""Interfaces with Alarm.com alarm control panels."""

from __future__ import annotations

import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from voluptuous import Any, Optional

from . import LoxoneEntity
from .catalog import control_is_selected, entity_is_selected, sauna_selection_key
from .helpers import (add_room_and_cat_to_value_values, get_all,
                      get_or_create_device)
from .miniserver import get_miniserver_from_hass

_LOGGER = logging.getLogger(__name__)

DEFAULT_FAN_SPEED_HOME = 30
DEFAULT_FAN_SPEED_AWAY = 10
DEFAULT_FAN_SPEED_BOOST = 100

VENTELATION_INT_TO_STR = {2: "Low", 3: "Medium", 4: "High", 5: "Auto", 6: "Away"}

DEFAULT_SPEED_BY_PRESET = {
    "Low": DEFAULT_FAN_SPEED_AWAY,
    "Medium": DEFAULT_FAN_SPEED_HOME,
    "High": DEFAULT_FAN_SPEED_BOOST,
    "Auto": DEFAULT_FAN_SPEED_HOME,
    "Away": DEFAULT_FAN_SPEED_AWAY,
}


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """
    For now, we do nothing. Function is only to get rid of the error message of missing async_setup_platform
    """
    pass


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up entry."""
    miniserver = get_miniserver_from_hass(hass, config_entry)
    loxconfig = miniserver.lox_config.json
    entities = []

    for fan in get_all(loxconfig, "Ventilation"):
        if not control_is_selected(config_entry, fan, "fan"):
            continue
        fan = add_room_and_cat_to_value_values(loxconfig, fan)
        fan.update(
            {
                "type": "ventilation",
                "async_add_devices": async_add_entities,
                "config_entry": config_entry,
            }
        )

        entities.append(LoxoneVentilation(**fan))

    from .sauna import LoxoneSaunaFan

    for sauna in get_all(loxconfig, "Sauna"):
        if not entity_is_selected(
            config_entry, sauna_selection_key(sauna["uuidAction"], "fan")
        ):
            continue
        sauna = add_room_and_cat_to_value_values(loxconfig, sauna)
        sauna.update(
            {
                "hass": hass,
                "config_entry_id": config_entry.entry_id,
                "gateway_id": config_entry.unique_id or config_entry.entry_id,
                "temperature_unit": loxconfig.get("msInfo", {}).get("tempUnit", 0),
            }
        )
        entities.append(LoxoneSaunaFan(**sauna))

    async_add_entities(entities)


class LoxoneVentilation(LoxoneEntity, FanEntity):
    """Representation of a ventilation Loxone device."""

    def __init__(self, **kwargs) -> None:
        """Initialize the fan."""
        super().__init__(**kwargs)

        self._device_class = None
        self._state = STATE_UNKNOWN
        self._format = self._get_format(kwargs.get("details", {}).get("format", ""))
        self._attr_available = True

        self._stateAttribUuids = kwargs["states"]
        self._stateAttribValues = {}
        self._details = kwargs["details"]
        self._mode_id_to_name: dict[int, str] = {}
        used_names: set[str] = set()
        for mode in self._details.get("modes", ()):
            if not isinstance(mode, dict):
                continue
            try:
                mode_id = int(mode["id"])
            except (KeyError, TypeError, ValueError):
                continue
            base_name = str(mode.get("name") or f"Mode {mode_id}")
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name} ({suffix})"
                suffix += 1
            used_names.add(name)
            self._mode_id_to_name[mode_id] = name
        if not self._mode_id_to_name:
            self._mode_id_to_name = dict(VENTELATION_INT_TO_STR)
        self._mode_name_to_id = {
            name: mode_id for mode_id, name in self._mode_id_to_name.items()
        }

        self.type = "Fan"
        self._attr_device_info = get_or_create_device(
            self.unique_id, self.name, self.type, self.room
        )

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes.

        Implemented by platform classes.
        """
        return {
            **self._attr_extra_state_attributes,
            "device_type": self.type,
        }

    @property
    def supported_features(self):
        """Flag supported features."""
        return FanEntityFeature.PRESET_MODE | FanEntityFeature.SET_SPEED

    async def event_handler(self, event):
        # _LOGGER.debug(f"Fan Event data: {event.data}")
        update = False

        for key in set(self._stateAttribUuids.values()) & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True

        if update:
            self.schedule_update_ha_state()

        # _LOGGER.debug(f"State attribs after event handling: {self._stateAttribValues}")

    @property
    def icon(self):
        """Return the fan icon."""
        return "mdi:fan"

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        if not hasattr(self, "_device_class"):
            return None
        else:
            return self._device_class

    @property
    def is_on(self) -> bool:
        """Return if device is on."""
        if self.percentage:
            return self.percentage > 0
        else:
            return False

    @property
    def preset_modes(self) -> list[str]:
        """Return a list of available preset modes."""
        return list(self._mode_name_to_id)

    @property
    def preset_mode(self) -> str | None:
        """Return a list of available preset modes."""
        try:
            mode_id = int(self.get_state_value("mode"))
        except (TypeError, ValueError):
            return None
        return self._mode_id_to_name.get(mode_id)

    @property
    def percentage(self) -> Optional[int]:
        """Return the current speed percentage."""
        return self.get_state_value("speed")

    @device_class.setter
    def device_class(self, device_class):
        if not hasattr(self, "_device_class"):
            setattr(self, "_device_class", device_class)
        else:
            self._device_class = device_class

    def get_state_value(self, name):
        uuid = self._stateAttribUuids[name]
        return (
            self._stateAttribValues[uuid] if uuid in self._stateAttribValues else None
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the preset mode of the fan."""
        if preset_mode not in self._mode_name_to_id:
            raise ValueError(f"Unsupported ventilation preset: {preset_mode}")
        percentage = self.percentage
        if percentage is None or percentage <= 0:
            percentage = DEFAULT_SPEED_BY_PRESET.get(
                preset_mode, DEFAULT_FAN_SPEED_HOME
            )
        await self._async_set_timer(percentage, preset_mode)

    async def _async_set_timer(self, percentage: int, preset_mode: str) -> None:
        """Send one complete ventilation timer command."""
        mode_id = self._mode_name_to_id[preset_mode]
        await self.async_send_command(
            self.uuidAction,
            f"setTimer/3600/{percentage}/{mode_id}/-1",
        )

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan."""
        await self._async_set_timer(percentage, self.preset_mode or "Auto")

    # def turn_on(self, speed: Optional[str] = None, percentage: Optional[int] = None, preset_mode: Optional[str] = None,
    #             **kwargs: Any) -> None:
    #     """Turn on the fan."""

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn the fan on."""
        effective_preset = preset_mode or self.preset_mode or "Auto"
        if effective_preset not in self._mode_name_to_id:
            raise ValueError(f"Unsupported ventilation preset: {effective_preset}")
        effective_percentage = percentage
        if effective_percentage is None:
            effective_percentage = self.percentage
        if effective_percentage is None or effective_percentage <= 0:
            effective_percentage = DEFAULT_SPEED_BY_PRESET.get(
                effective_preset, DEFAULT_FAN_SPEED_HOME
            )
        await self._async_set_timer(effective_percentage, effective_preset)
        _LOGGER.debug("Turn on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the fan off."""
        if not self.is_on:
            return
        await self.async_set_percentage(0)
