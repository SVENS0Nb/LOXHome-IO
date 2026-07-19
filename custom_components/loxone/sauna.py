"""Home Assistant entities for the Loxone Sauna function block."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.fan import FanEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime

from . import LoxoneEntity
from .catalog import feature_is_enabled
from .helpers import get_or_create_device

SAUNA_MODES: dict[int, str] = {
    0: "Aus",
    1: "Finnisch manuell",
    2: "Feuchtebetrieb manuell",
    3: "Finnisch automatisch",
    4: "Kräutersauna",
    5: "Soft-Dampfbad",
    6: "Warmluftbad",
}


def _as_bool(value: Any) -> bool | None:
    """Convert numeric and textual Loxone digital values safely."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"0", "0.0", "false", "off", "no", ""}:
            return False
        if normalized in {"1", "1.0", "true", "on", "yes"}:
            return True
        try:
            return float(normalized) != 0
        except ValueError:
            pass
    return bool(value)


def _as_float(value: Any) -> float | None:
    """Convert numeric Loxone values without leaking invalid text into HA."""
    try:
        converted = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return converted if converted is not None and math.isfinite(converted) else None


class LoxoneSaunaEntity(LoxoneEntity):
    """Common event subscription and command handling for a Sauna device."""

    _suffix = "entity"
    _name_suffix = ""
    _required_states: tuple[str, ...] = ()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.hass = kwargs["hass"]
        self._sauna_uuid = str(kwargs["uuidAction"])
        self._base_name = str(kwargs.get("name", "Sauna"))
        self._state_uuids: dict[str, str] = dict(kwargs.get("states", {}))
        self._state_values: dict[str, Any] = {}
        self._temperature_unit = (
            UnitOfTemperature.FAHRENHEIT
            if int(kwargs.get("temperature_unit", 0)) == 1
            else UnitOfTemperature.CELSIUS
        )
        self._attr_should_poll = False
        self._attr_available = False
        self._attr_name = self._base_name if not self._name_suffix else f"{self._base_name} {self._name_suffix}"
        self._attr_device_info = get_or_create_device(
            f"{self._gateway_id}-{self._sauna_uuid}",
            self._base_name,
            "Sauna",
            str(kwargs.get("room", "")),
        )

    @property
    def unique_id(self) -> str:
        """Return a stable ID for each Sauna capability."""
        return f"{self._gateway_id}-{self._sauna_uuid}-{self._suffix}"

    async def event_handler(self, event) -> None:
        """Store every state update belonging to this Sauna block."""
        changed = False
        for state_name, state_uuid in self._state_uuids.items():
            if state_uuid in event.data:
                self._state_values[state_name] = event.data[state_uuid]
                changed = True
        if changed:
            self._attr_available = all(
                state_name in self._state_values
                for state_name in self._required_states
            )
            self.async_write_ha_state()

    def state_value(self, state_name: str) -> Any:
        """Return a state value received from the Miniserver."""
        return self._state_values.get(state_name)

    async def _send(self, command: str) -> None:
        await self.async_send_command(self._sauna_uuid, command)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self._attr_extra_state_attributes,
            "device_type": "Sauna",
            "source_uuid": self._sauna_uuid,
        }


class LoxoneSaunaClimate(LoxoneSaunaEntity, ClimateEntity):
    """Main Sauna temperature control."""

    _suffix = "climate"
    _required_states = ("active", "power", "tempActual", "tempTarget")
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 1.0
    _attr_min_temp = 30.0
    _attr_max_temp = 120.0

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self._temperature_unit == UnitOfTemperature.FAHRENHEIT:
            self._attr_min_temp = 86.0
            self._attr_max_temp = 248.0

    @property
    def temperature_unit(self) -> str:
        return self._temperature_unit

    @property
    def current_temperature(self) -> float | None:
        return _as_float(self.state_value("tempActual"))

    @property
    def target_temperature(self) -> float | None:
        return _as_float(self.state_value("tempTarget"))

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.HEAT if _as_bool(self.state_value("active")) else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        if not _as_bool(self.state_value("active")):
            return HVACAction.OFF
        return (
            HVACAction.HEATING
            if _as_bool(self.state_value("power"))
            else HVACAction.IDLE
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get("temperature")) is not None:
            await self._send(f"temp/{temperature}")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._send("on" if hvac_mode == HVACMode.HEAT else "off")

    async def async_turn_on(self) -> None:
        await self._send("on")

    async def async_turn_off(self) -> None:
        await self._send("off")


class LoxoneSaunaFan(LoxoneSaunaEntity, FanEntity):
    """Sauna ventilation switch."""

    _suffix = "fan"
    _name_suffix = "Lüftung"
    _required_states = ("fan",)

    @property
    def is_on(self) -> bool | None:
        value = self.state_value("fan")
        return _as_bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send("fanon")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send("fanoff")


class LoxoneSaunaModeSelect(LoxoneSaunaEntity, SelectEntity):
    """Sauna operating mode selector."""

    _suffix = "mode"
    _name_suffix = "Betriebsart"
    _required_states = ("mode",)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        has_vaporizer = feature_is_enabled(
            (kwargs.get("details") or {}).get("hasVaporizer")
        )
        allowed = range(0, 7) if has_vaporizer else (0, 1, 3)
        self._mode_to_name = {mode: SAUNA_MODES[mode] for mode in allowed}
        self._name_to_mode = {name: mode for mode, name in self._mode_to_name.items()}
        self._attr_options = list(self._name_to_mode)

    @property
    def current_option(self) -> str | None:
        value = self.state_value("mode")
        try:
            return self._mode_to_name.get(int(value))
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        await self._send(f"mode/{self._name_to_mode[option]}")


class LoxoneSaunaHumidityNumber(LoxoneSaunaEntity, NumberEntity):
    """Sauna target humidity."""

    _suffix = "humidity-target"
    _name_suffix = "Zielfeuchtigkeit"
    _required_states = ("humidityTarget",)
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> float | None:
        return _as_float(self.state_value("humidityTarget"))

    async def async_set_native_value(self, value: float) -> None:
        await self._send(f"humidity/{value:g}")


class LoxoneSaunaSensor(LoxoneSaunaEntity, SensorEntity):
    """A selected numeric Sauna state."""

    def __init__(
        self,
        *,
        state_name: str,
        suffix: str,
        name_suffix: str,
        device_class: SensorDeviceClass | None = None,
        unit: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._suffix = suffix
        self._name_suffix = name_suffix
        self._state_name = state_name
        self._required_states = (state_name,)
        super().__init__(**kwargs)
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = (
            self._temperature_unit
            if device_class == SensorDeviceClass.TEMPERATURE
            else unit
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Any:
        return _as_float(self.state_value(self._state_name))


class LoxoneSaunaBinarySensor(LoxoneSaunaEntity, BinarySensorEntity):
    """A selected digital Sauna state."""

    def __init__(
        self,
        *,
        state_name: str,
        suffix: str,
        name_suffix: str,
        device_class: BinarySensorDeviceClass | None = None,
        invert: bool = False,
        **kwargs: Any,
    ) -> None:
        self._suffix = suffix
        self._name_suffix = name_suffix
        self._state_name = state_name
        self._required_states = (state_name,)
        self._invert = invert
        super().__init__(**kwargs)
        self._attr_device_class = device_class

    @property
    def is_on(self) -> bool | None:
        value = self.state_value(self._state_name)
        if value is None:
            return None
        result = _as_bool(value)
        if result is None:
            return None
        return not result if self._invert else result


class LoxoneSaunaTimerButton(LoxoneSaunaEntity, ButtonEntity):
    """Start the Sauna sand timer."""

    _suffix = "start-timer"
    _name_suffix = "Timer starten"
    _required_states = ("timer",)

    async def async_press(self) -> None:
        await self._send("starttimer")


SAUNA_SENSOR_DEFINITIONS: dict[str, dict[str, Any]] = {
    "temp_actual": {
        "state_name": "tempActual",
        "suffix": "temperature",
        "name_suffix": "Temperatur",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
    },
    "temp_bench": {
        "state_name": "tempBench",
        "suffix": "bench-temperature",
        "name_suffix": "Banktemperatur",
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
    },
    "humidity_actual": {
        "state_name": "humidityActual",
        "suffix": "humidity",
        "name_suffix": "Luftfeuchtigkeit",
        "device_class": SensorDeviceClass.HUMIDITY,
        "unit": PERCENTAGE,
    },
    "timer": {
        "state_name": "timer",
        "suffix": "timer",
        "name_suffix": "Restlaufzeit",
        "device_class": SensorDeviceClass.DURATION,
        "unit": UnitOfTime.SECONDS,
    },
}

SAUNA_BINARY_SENSOR_DEFINITIONS: dict[str, dict[str, Any]] = {
    "heating": {
        "state_name": "power",
        "suffix": "heating",
        "name_suffix": "Heizung",
        "device_class": BinarySensorDeviceClass.HEAT,
    },
    "drying": {
        "state_name": "drying",
        "suffix": "drying",
        "name_suffix": "Trocknung",
        "device_class": BinarySensorDeviceClass.RUNNING,
    },
    "door": {
        "state_name": "doorClosed",
        "suffix": "door",
        "name_suffix": "Tür",
        "device_class": BinarySensorDeviceClass.DOOR,
        "invert": True,
    },
    "presence": {
        "state_name": "presence",
        "suffix": "presence",
        "name_suffix": "Präsenz",
        "device_class": BinarySensorDeviceClass.PRESENCE,
    },
    "error": {
        "state_name": "error",
        "suffix": "error",
        "name_suffix": "Störung",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
    "low_water": {
        "state_name": "lessWater",
        "suffix": "low-water",
        "name_suffix": "Wassermangel",
        "device_class": BinarySensorDeviceClass.PROBLEM,
    },
}
