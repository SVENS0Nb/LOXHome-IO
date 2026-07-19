"""Support for Loxone binary sensors."""

from __future__ import annotations

import logging
from typing import Literal, final

from homeassistant.components.binary_sensor import (BinarySensorDeviceClass,
                                                    BinarySensorEntity)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (CONF_VALUE_TEMPLATE,
                                 STATE_OFF, STATE_ON, STATE_UNKNOWN)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import LoxoneEntity
from .catalog import (
    control_is_selected,
    entity_is_selected,
    feature_is_enabled,
    sauna_selection_key,
    subcontrol_selection_key,
)
from .helpers import (add_room_and_cat_to_value_values, get_all,
                      get_or_create_device)
from .miniserver import get_miniserver_from_hass

_LOGGER = logging.getLogger(__name__)
NEW_SENSOR = "binairy_sensors"
DEFAULT_NAME = "Loxone Binary Sensor"

LOXONE_DEVICE_CLASS_MAP: dict[str, BinarySensorDeviceClass] = {
    "presence": BinarySensorDeviceClass.PRESENCE,
    "smoke": BinarySensorDeviceClass.SMOKE,
}


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Loxone Sensor from yaml"""
    value_template = config.get(CONF_VALUE_TEMPLATE)
    if value_template is not None:
        value_template.hass = hass

    # Devices from yaml
    if config != {}:
        # Here setup all Sensors in Yaml-File
        new_sensor = LoxoneCustomBinarySensor(**config)
        async_add_devices([new_sensor])
        return True
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up entry."""
    miniserver = get_miniserver_from_hass(hass, config_entry)
    loxconfig = miniserver.lox_config.json
    entities = []

    for sensor in get_all(loxconfig, "InfoOnlyDigital"):
        if not control_is_selected(config_entry, sensor, "binary_sensor"):
            continue
        sensor = add_room_and_cat_to_value_values(loxconfig, sensor)
        sensor.update({"type": "digital"})
        entities.append(LoxoneDigitalSensor(**sensor))

    for sensor in get_all(loxconfig, "PresenceDetector"):
        if not control_is_selected(config_entry, sensor, "binary_sensor"):
            continue
        sensor = add_room_and_cat_to_value_values(loxconfig, sensor)
        sensor.update({"type": "presence"})
        entities.append(LoxoneDigitalSensor(**sensor))

    for sensor in get_all(loxconfig, "SmokeAlarm"):
        if not control_is_selected(config_entry, sensor, "binary_sensor"):
            continue
        sensor = add_room_and_cat_to_value_values(loxconfig, sensor)
        sensor.update({"type": "smoke"})
        entities.append(LoxoneDigitalSensor(**sensor))

    for ventilation in get_all(loxconfig, "Ventilation"):
        state_uuid = ventilation.get("states", {}).get("presence")
        if (
            not state_uuid
            or not feature_is_enabled(
                ventilation.get("details", {}).get("hasPresence")
            )
            or not entity_is_selected(
                config_entry,
                subcontrol_selection_key(state_uuid, "binary_sensor"),
            )
        ):
            continue
        ventilation = add_room_and_cat_to_value_values(loxconfig, ventilation)
        entities.append(
            LoxoneDigitalSensor(
                parent_id=ventilation["uuidAction"],
                uuidAction=state_uuid,
                type="presence",
                room=ventilation.get("room", ""),
                cat=ventilation.get("cat", ""),
                name=f"{ventilation['name']} - Präsenz",
                device_class="presence",
                async_add_devices=async_add_entities,
                config_entry=config_entry,
            )
        )

    from .sauna import (
        LoxoneSaunaBinarySensor,
        SAUNA_BINARY_SENSOR_DEFINITIONS,
    )

    for sauna in get_all(loxconfig, "Sauna"):
        sauna_uuid = sauna["uuidAction"]
        sauna = add_room_and_cat_to_value_values(loxconfig, sauna)
        sauna.update(
            {
                "hass": hass,
                "config_entry_id": config_entry.entry_id,
                "gateway_id": config_entry.unique_id or config_entry.entry_id,
                "temperature_unit": loxconfig.get("msInfo", {}).get("tempUnit", 0),
            }
        )
        for suffix, definition in SAUNA_BINARY_SENSOR_DEFINITIONS.items():
            if definition["state_name"] not in sauna.get("states", {}):
                continue
            if suffix == "door" and not feature_is_enabled(
                sauna.get("details", {}).get("hasDoorSensor")
            ):
                continue
            if suffix == "low_water" and not feature_is_enabled(
                sauna.get("details", {}).get("hasVaporizer")
            ):
                continue
            if not entity_is_selected(
                config_entry, sauna_selection_key(sauna_uuid, suffix)
            ):
                continue
            entities.append(LoxoneSaunaBinarySensor(**definition, **sauna))

    @callback
    def async_add_binary_sensors(_):
        async_add_entities(_, True)

    miniserver.listeners.append(
        async_dispatcher_connect(
            hass,
            miniserver.async_signal_new_device("sensors"),
            async_add_binary_sensors,
        )
    )
    async_add_entities(entities)


class LoxoneDigitalSensor(LoxoneEntity, BinarySensorEntity):
    """Representation of a binary Loxone device."""

    _attr_is_on: bool | None = None
    _attr_state: None = None
    _attr_available = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._attr_state = STATE_UNKNOWN
        self._attr_is_on = STATE_UNKNOWN
        self._from_loxone_config = False

        if (
            "type" in kwargs
            and "room" in kwargs
            and "cat" in kwargs
            and hasattr(self, "states")
        ):
            self._from_loxone_config = True
            if self.type == "smoke":
                self._state_uuid = self.states["areAlarmSignalsOff"]
            if self.type == "presence":
                self._state_uuid = self.states["active"]
            elif "active" in self.states:
                self._state_uuid = self.uuidAction
        else:
            self._state_uuid = self.uuidAction

        self._state = STATE_UNKNOWN
        self._format = self._get_format(kwargs.get("details", {}).get("format", ""))
        self._parent_id = kwargs.get("parent_id", None)
        self._on_state = STATE_ON
        self._off_state = STATE_OFF
        self._attr_available = True
        if self.type in LOXONE_DEVICE_CLASS_MAP:
            self._attr_device_class = LOXONE_DEVICE_CLASS_MAP[self.type]
        else:
            self._attr_device_class = None

        if self._parent_id:
            self.uuidAction = self._parent_id

        if self._from_loxone_config:
            self._attr_device_info = get_or_create_device(
                self.unique_id, self.name, self.type, self.room
            )
        else:
            self._attr_device_info = get_or_create_device(
                self.unique_id, self.name, self.type, ""
            )

        if self._from_loxone_config:
            self._attr_extra_state_attributes.update(
                {
                    "state_uuid": self._state_uuid,
                    "device_type": self.type,
                }
            )
        else:
            self._attr_extra_state_attributes.update(
                {
                    "device_type": self._attr_device_class,
                }
            )

    async def event_handler(self, e):
        if self._state_uuid in e.data:
            self._state = e.data[self._state_uuid]
            if self._state == 1.0:
                self._state = self._on_state
            else:
                self._state = self._off_state
            if not self._attr_available:
                self._attr_available = True
            self.async_schedule_update_ha_state()

    @final
    @property
    def state(self) -> Literal["on", "off"] | None:
        """Return the state of the binary sensor."""
        if (is_on := self.is_on) is None:
            return None
        return STATE_ON if is_on else STATE_OFF

    @property
    def is_on(self) -> bool | None:
        """Return true if sensor is on."""
        return self._state == self._on_state


class LoxoneCustomBinarySensor(LoxoneEntity, BinarySensorEntity):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._name = kwargs["name"]
        self._state = STATE_UNKNOWN
        self._on_state = STATE_ON
        self._off_state = STATE_OFF

        if "uuidAction" in kwargs:
            self.uuidAction = kwargs["uuidAction"]
        else:
            self.uuidAction = ""

    @property
    def is_on(self) -> bool | None:
        """Return true if sensor is on."""
        return self._state == self._on_state

    @property
    def state(self) -> Literal["on", "off"] | None:
        """Return the state of the binary sensor."""
        if (is_on := self.is_on) is None:
            return None
        return STATE_ON if is_on else STATE_OFF

    async def event_handler(self, e):
        if self.uuidAction in e.data:
            data = e.data[self.uuidAction]
            if data == 1.0:
                self._state = self._on_state
            else:
                self._state = self._off_state
            self.async_schedule_update_ha_state()

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name
