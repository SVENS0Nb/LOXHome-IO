"""
Loxone climate

For more details about this component, please refer to the documentation at
https://github.com/JoDehli/PyLoxone
"""

import json
import logging
from abc import ABC

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (ClimateEntityFeature,
                                                    HVACAction, HVACMode)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from voluptuous import All, Optional, Range

from . import LoxoneEntity
from .catalog import control_is_selected, entity_is_selected, sauna_selection_key
from .const import CONF_HVAC_AUTO_MODE
from .helpers import (add_room_and_cat_to_value_values, get_all,
                      get_or_create_device)
from .miniserver import get_miniserver_from_hass

_LOGGER = logging.getLogger(__name__)


def _json_object_list(value) -> list[dict]:
    """Return a validated list of JSON objects from a Loxone text state."""
    if isinstance(value, list):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
    else:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


OPMODES = {
    None: HVACMode.OFF,
    -1: HVACMode.OFF,
    0: HVACMode.AUTO,
    1: HVACMode.AUTO,
    2: HVACMode.AUTO,
    3: HVACMode.HEAT_COOL,
    4: HVACMode.HEAT,
    5: HVACMode.HEAT_COOL,
}

OPMODETOLOXONE = {
    HVACMode.HEAT_COOL: 3,
    HVACMode.HEAT: 4,
    HVACMode.COOL: 5,
    HVACMode.OFF: -1,
}


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        Optional(CONF_HVAC_AUTO_MODE, default=0): All(int, Range(min=0, max=2)),
    }
)


# noinspection PyUnusedLocal
async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    # value_template = config.get(CONF_VALUE_TEMPLATE)
    # auto_mode = 0 if config.get(CONF_HVAC_AUTO_MODE) is None else config.get(CONF_HVAC_AUTO_MODE)
    #
    # if value_template is not None:
    #     value_template.hass = hass
    # config = hass.data[DOMAIN]
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LoxoneRoomControllerV2."""
    miniserver = get_miniserver_from_hass(hass, config_entry)
    loxconfig = miniserver.lox_config.json
    entities = []

    for climate in get_all(loxconfig, "IRoomControllerV2"):
        if not control_is_selected(config_entry, climate, "climate"):
            continue
        climate = add_room_and_cat_to_value_values(loxconfig, climate)
        climate.update(
            {
                "hass": hass,
                CONF_HVAC_AUTO_MODE: 0,
            }
        )
        entities.append(LoxoneRoomControllerV2(**climate))

    for climate in get_all(loxconfig, "IRoomController"):
        if not control_is_selected(config_entry, climate, "climate"):
            continue
        climate = add_room_and_cat_to_value_values(loxconfig, climate)
        climate.update(
            {
                "hass": hass,
                CONF_HVAC_AUTO_MODE: 0,
            }
        )
        entities.append(LoxoneRoomController(**climate))

    for accontrol in get_all(loxconfig, "AcControl"):
        if not control_is_selected(config_entry, accontrol, "climate"):
            continue
        accontrol = add_room_and_cat_to_value_values(loxconfig, accontrol)
        accontrol.update(
            {
                "hass": hass,
            }
        )
        entities.append(LoxoneAcControl(**accontrol))

    from .sauna import LoxoneSaunaClimate

    for sauna in get_all(loxconfig, "Sauna"):
        if not entity_is_selected(
            config_entry, sauna_selection_key(sauna["uuidAction"], "climate")
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
        entities.append(LoxoneSaunaClimate(**sauna))

    async_add_entities(entities)


class LoxoneRoomController(LoxoneEntity, ClimateEntity, ABC):
    """Loxone room controller (legacy, non-V2)"""

    def __init__(self, **kwargs):
        # Add room name to entity name for better identification in HomeKit
        if "room" in kwargs and kwargs["room"]:
            kwargs["name"] = f"{kwargs['room']} Climate"

        super().__init__(**kwargs)
        self.hass = kwargs["hass"]
        self._autoMode = kwargs[CONF_HVAC_AUTO_MODE]
        self._stateAttribUuids = kwargs["states"]
        self._stateAttribValues = {}
        self.type = "RoomController"

        # Set supported features
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

        # Flatten UUID values - some might be lists (e.g., "temperatures")
        self._all_uuids = set()
        for value in self._stateAttribUuids.values():
            if isinstance(value, list):
                self._all_uuids.update(value)
            else:
                self._all_uuids.add(value)

        self._attr_device_info = get_or_create_device(
            self.unique_id, self.name, self.type, self.room
        )

    async def event_handler(self, event):
        update = False

        for key in self._all_uuids & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True

        if update:
            self.async_write_ha_state()

    def get_state_value(self, name):
        uuid = self._stateAttribUuids.get(name)
        if isinstance(uuid, list):
            # For "temperatures" which is a list of UUIDs
            return [
                self._stateAttribValues.get(u)
                for u in uuid
                if u in self._stateAttribValues
            ]
        return (
            self._stateAttribValues[uuid]
            if uuid and uuid in self._stateAttribValues
            else None
        )

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes."""
        return {
            **self._attr_extra_state_attributes,
            "mode": self.get_state_value("mode"),
            "override": self.get_state_value("override"),
            "open_window": self.get_state_value("openWindow"),
            "curr_heat_temp_ix": self.get_state_value("currHeatTempIx"),
            "curr_cool_temp_ix": self.get_state_value("currCoolTempIx"),
        }

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self.get_state_value("tempActual")

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self.get_state_value("tempTarget")

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature"""
        temp = kwargs.get("temperature")
        if temp is None:
            return

        # IRoomController uses setTemp with current temperature index
        # Get the current active temperature index based on mode
        mode = self.get_state_value("mode")

        # Determine which temperature index to use
        temp_idx = self.get_state_value("currHeatTempIx")
        if mode == 2:  # Cooling mode
            cool_idx = self.get_state_value("currCoolTempIx")
            if cool_idx is not None:
                temp_idx = cool_idx

        if temp_idx is not None:
            # Command format: setTemp/<index>/<value>
            await self.async_send_command(
                self.uuidAction, f"setTemp/{int(temp_idx)}/{temp}"
            )
            self.schedule_update_ha_state()

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action (heating, cooling)."""
        valve_heat = self.get_state_value("valveHeat")
        valve_cool = self.get_state_value("valveCool")

        if valve_heat and valve_heat > 0:
            return HVACAction.HEATING
        elif valve_cool and valve_cool > 0:
            return HVACAction.COOLING

        if self.get_state_value("isPreparing") == 1:
            return HVACAction.PREHEATING

        return HVACAction.IDLE

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation mode."""
        mode = self.get_state_value("mode")

        # mode: 0=Auto, 1=Heat, 2=Cool, 3=Heat/Cool, 4=Off
        if mode == 0:
            return HVACMode.AUTO
        elif mode == 1:
            return HVACMode.HEAT
        elif mode == 2:
            return HVACMode.COOL
        elif mode == 3:
            return HVACMode.HEAT_COOL
        else:
            return HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available hvac operation modes."""
        return [
            HVACMode.OFF,
            HVACMode.AUTO,
            HVACMode.HEAT,
            HVACMode.COOL,
            HVACMode.HEAT_COOL,
        ]

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement used by the platform."""
        format_str = self.details.get("format")

        if format_str is None:
            return UnitOfTemperature.CELSIUS

        if "°F" in format_str or "F" in format_str:
            return UnitOfTemperature.FAHRENHEIT

        if "°C" in format_str or "C" in format_str:
            return UnitOfTemperature.CELSIUS

        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature_step(self) -> float | None:
        """Return the supported step of target temperature."""
        return 0.5

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return 7.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return 35.0

    async def async_set_hvac_mode(self, hvac_mode: str):
        """Set new target hvac mode."""
        # Map HVAC mode to Loxone mode
        mode_map = {
            HVACMode.OFF: 4,
            HVACMode.AUTO: 0,
            HVACMode.HEAT: 1,
            HVACMode.COOL: 2,
            HVACMode.HEAT_COOL: 3,
        }

        target_mode = mode_map.get(hvac_mode, 0)

        await self.async_send_command(self.uuidAction, f"setMode/{target_mode}")

        self.schedule_update_ha_state()


class LoxoneRoomControllerV2(LoxoneEntity, ClimateEntity, ABC):
    """Loxone room controller"""

    _attr_supported_features = (
        ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hass = kwargs["hass"]
        self._autoMode = kwargs[CONF_HVAC_AUTO_MODE]
        self._stateAttribUuids = kwargs["states"]
        self._stateAttribValues = {}
        self.type = "RoomControllerV2"
        self._modeList = [
            mode
            for mode in kwargs["details"].get("timerModes", ())
            if isinstance(mode, dict) and "id" in mode and "name" in mode
        ]
        self._attr_available = False

        self._attr_device_info = get_or_create_device(
            self.unique_id, self.name, self.type, self.room
        )

    def get_mode_from_id(self, mode_id):
        for mode in self._modeList:
            if mode.get("id") == mode_id:
                return mode["name"]
        return None

    async def event_handler(self, event):
        # _LOGGER.debug(f"Climate Event data: {event.data}")
        update = False

        for key in set(self._stateAttribUuids.values()) & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True

        if update:
            self._attr_available = self.get_state_value("tempActual") is not None
            self.schedule_update_ha_state()

        # _LOGGER.debug(f"State attribs after event handling: {self._stateAttribValues}")

    def get_state_value(self, name):
        uuid = self._stateAttribUuids[name]
        return (
            self._stateAttribValues[uuid] if uuid in self._stateAttribValues else None
        )

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes.

        Implemented by platform classes.
        """
        return {
            **self._attr_extra_state_attributes,
            "is_overridden": self.is_overridden,
        }

    @property
    def is_overridden(self) -> bool:
        _override_entries = self.get_state_value("overrideEntries")
        if _override_entries:
            try:
                _override_entries = json.loads(_override_entries)
            except (TypeError, json.JSONDecodeError):
                return False
            if isinstance(_override_entries, list) and len(_override_entries) > 0:
                return True
        return False

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self.get_state_value("tempActual")

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature"""
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        operating_mode = self.get_state_value("operatingMode")
        if operating_mode is None:
            raise HomeAssistantError("Loxone climate state is not available yet")
        if operating_mode > 2:  # Set manual temp if any manual mode is selected
            await self.async_send_command(
                self.uuidAction,
                f"setManualTemperature/{temperature}",
            )
        else:  # Set comfort temp offset otherwise
            comfort_temperature = self.get_state_value("comfortTemperature")
            if comfort_temperature is None:
                raise HomeAssistantError("Loxone comfort temperature is unavailable")
            new_offset = temperature - comfort_temperature
            await self.async_send_command(
                self.uuidAction, f"setComfortModeTemp/{new_offset}"
            )

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action (heating, cooling)."""
        if self.get_state_value("prepareState") == 1:
            return HVACAction.PREHEATING
        return None  # return none due to unknown other state (HVACAction.IDLE, HVACAction.COOLING, HVACAction.HEATING)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation ie. heat, cool mode.

        Need to be one of HVAC_MODE_*.
        """
        return OPMODES.get(self.get_state_value("operatingMode"), HVACMode.OFF)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available hvac operation modes.

        Need to be a subset of HVAC_MODES.
        """
        return [
            HVACMode.AUTO,
            HVACMode.HEAT,
            HVACMode.HEAT_COOL,
            HVACMode.COOL,
            HVACMode.OFF,
        ]

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement used by the platform."""
        # The Loxone Config app allows the designer to set an arbitrary
        # format string for the room controller's input temperature sensor.
        # We assume that the format string contains the unit of temperature,
        # and default to Celsius if not.
        format_str = self.details.get("format")

        if format_str is None:
            return UnitOfTemperature.CELSIUS

        if "°F" in format_str or "F" in format_str:
            return UnitOfTemperature.FAHRENHEIT

        if "°C" in format_str or "C" in format_str:
            return UnitOfTemperature.CELSIUS

        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""

        return self.get_state_value("tempTarget")

    @property
    def target_temperature_step(self) -> float | None:
        """Return the supported step of target temperature."""
        return 0.5

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp.

        Requires SUPPORT_PRESET_MODE.
        """
        # return self._activeMode
        return self.get_mode_from_id(self.get_state_value("activeMode"))

    @property
    def preset_modes(self):
        """Return a list of available preset modes.

        Requires SUPPORT_PRESET_MODE.
        """
        return [mode["name"] for mode in self._modeList]

    async def async_set_hvac_mode(self, hvac_mode: str):
        """Set new target hvac mode."""

        target_mode = (
            self._autoMode if hvac_mode == HVACMode.AUTO else OPMODETOLOXONE[hvac_mode]
        )

        await self.async_send_command(
            self.uuidAction, f"setOperatingMode/{target_mode}"
        )

        self.schedule_update_ha_state()

        # if the mode selected is a manual one, we set the target temperature too
        # if (hvac_mode != HVAC_MODE_AUTO):
        #    self.set_temperature({"temperature": self.target_temperature})

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        mode_id = next(
            (mode["id"] for mode in self._modeList if mode["name"] == preset_mode), None
        )
        if mode_id is not None:
            await self.async_send_command(self.uuidAction, f"override/{mode_id}")
            self.schedule_update_ha_state()


# ------------------ AC CONTROL --------------------------------------------------------
class LoxoneAcControl(LoxoneEntity, ClimateEntity, ABC):
    """Representation of a ACControl Loxone device."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hass = kwargs["hass"]

        self._stateAttribUuids = kwargs["states"]
        self._stateAttribValues = {}
        self._attr_available = False
        self.type = "AcControl"
        self._attr_device_info = get_or_create_device(
            self.unique_id, self.name, self.type, self.room
        )

    async def event_handler(self, event):
        # _LOGGER.debug(f"Climate Event data: {event.data}")
        update = False

        for key in set(self._stateAttribUuids.values()) & event.data.keys():
            self._stateAttribValues[key] = event.data[key]
            update = True

        if update:
            self._attr_available = self.get_state_value("status") is not None
            self.schedule_update_ha_state()

        # _LOGGER.debug(f"State attribs after event handling: {self._stateAttribValues}")

    def get_state_value(self, name):
        uuid = self._stateAttribUuids[name]
        return (
            self._stateAttribValues[uuid] if uuid in self._stateAttribValues else None
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
    def current_temperature(self):
        """Return the current temperature."""
        return self.get_state_value("temperature")

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature"""
        temp = kwargs.get("temperature")
        if temp is None:
            return
        await self.async_send_command(self.uuidAction, f"setTarget/{temp}")

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation ie. heat, cool mode.

        Need to be one of HVAC_MODE_*.
        """
        if self.get_state_value("status"):
            if self.get_state_value("mode") == 2:
                return HVACMode.HEAT
            elif self.get_state_value("mode") == 3:
                return HVACMode.COOL
            elif self.get_state_value("mode") == 4:
                return HVACMode.DRY
            elif self.get_state_value("mode") == 5:
                return HVACMode.FAN_ONLY
            else:
                return HVACMode.AUTO
        return HVACMode.OFF

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target hvac mode."""

        mode = 1
        match hvac_mode:
            case HVACMode.HEAT:
                mode = 2
            case HVACMode.COOL:
                mode = 3
            case HVACMode.DRY:
                mode = 4
            case HVACMode.FAN_ONLY:
                mode = 5

        await self.async_send_command(
            self.uuidAction,
            "off" if hvac_mode == HVACMode.OFF else "on",
        )
        if hvac_mode == HVACMode.OFF:
            return
        await self.async_send_command(self.uuidAction, f"setMode/{mode}")

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available hvac operation modes.

        Need to be a subset of HVAC_MODES.
        """
        return [
            HVACMode.OFF,
            HVACMode.HEAT,
            HVACMode.COOL,
            HVACMode.DRY,
            HVACMode.FAN_ONLY,
            HVACMode.AUTO,
        ]

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement used by the platform."""
        format_string = str(self.details.get("format", ""))
        if "°F" in format_string or " F" in format_string:
            return UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""

        return self.get_state_value("targetTemperature")

    @property
    def target_temperature_step(self) -> float | None:
        """Return the supported step of target temperature."""
        return 0.5

    @property
    def fan_mode(self) -> str | None:
        """Return current fan mode."""

        for mode in _json_object_list(self.get_state_value("fanspeeds")):
            if self.get_state_value("fan") == mode.get("id"):
                return mode.get("name")

        return "Auto"

    async def async_set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        mode_id = next(
            (
                option.get("id")
                for option in _json_object_list(self.get_state_value("fanspeeds"))
                if option.get("name") == fan_mode
            ),
            None,
        )
        if mode_id is None:
            raise HomeAssistantError(f"Unknown Loxone fan mode: {fan_mode}")
        await self.async_send_command(self.uuidAction, f"setFan/{mode_id}")

    @property
    def fan_modes(self) -> list[str]:
        """Return the list of available hvac operation modes."""

        return [
            str(option["name"])
            for option in _json_object_list(self.get_state_value("fanspeeds"))
            if "name" in option
        ]

    @property
    def swing_mode(self) -> str | None:
        """Return current swing mode."""

        for mode in _json_object_list(self.get_state_value("airflows")):
            if self.get_state_value("ventMode") == mode.get("id"):
                return mode.get("name")

        return "Auto"

    async def async_set_swing_mode(self, swing_mode):
        """Set new target swing mode."""

        mode_id = next(
            (
                option.get("id")
                for option in _json_object_list(self.get_state_value("airflows"))
                if option.get("name") == swing_mode
            ),
            None,
        )
        if mode_id is None:
            raise HomeAssistantError(f"Unknown Loxone swing mode: {swing_mode}")
        await self.async_send_command(self.uuidAction, f"setAirDir/{mode_id}")

    @property
    def swing_modes(self) -> list[str]:
        """Return the list of available swing modes."""

        return [
            str(option["name"])
            for option in _json_object_list(self.get_state_value("airflows"))
            if "name" in option
        ]
