"""
Loxone Numbers

For more details about this component, please refer to the documentation at
https://github.com/JoDehli/PyLoxone
"""

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import LoxoneEntity
from .catalog import (
    control_is_selected,
    entity_is_selected,
    feature_is_enabled,
    sauna_selection_key,
)
from .helpers import (add_room_and_cat_to_value_values, get_all,
                      get_or_create_device)
from .miniserver import get_miniserver_from_hass

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Loxone Number."""
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

    for number_entity in get_all(loxconfig, ["Slider"]):
        if not control_is_selected(config_entry, number_entity, "number"):
            continue
        number_entity = add_room_and_cat_to_value_values(loxconfig, number_entity)
        new_number = LoxoneNumber(**number_entity)
        entities.append(new_number)

    from .sauna import LoxoneSaunaHumidityNumber

    for sauna in get_all(loxconfig, "Sauna"):
        if (
            "humidityTarget" not in sauna.get("states", {})
            or not feature_is_enabled(sauna.get("details", {}).get("hasVaporizer"))
            or not entity_is_selected(
                config_entry,
                sauna_selection_key(sauna["uuidAction"], "humidity_target"),
            )
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
        entities.append(LoxoneSaunaHumidityNumber(**sauna))

    async_add_entities(entities)


class LoxoneNumber(LoxoneEntity, NumberEntity):
    """Representation of a loxone number"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        """Initialize the Loxone number."""
        self._state = STATE_UNKNOWN
        self._icon = None
        self._assumed = False
        self._native_max_value = kwargs["details"]["max"]
        self._native_min_value = kwargs["details"]["min"]
        self._native_step = kwargs["details"]["step"]

        self.type = "Slider"
        self._attr_device_info = get_or_create_device(
            self.unique_id, self.name, self.type, self.room
        )

    @property
    def should_poll(self):
        """No polling needed for a demo number."""
        return False

    @property
    def icon(self):
        """Return the icon to use for device if any."""
        return self._icon

    @property
    def native_max_value(self):
        """Return the native_max_value to use for device if any."""
        return self._native_max_value

    @property
    def native_min_value(self):
        """Return the native_min_value to use for device if any."""
        return self._native_min_value

    @property
    def native_step(self):
        """Return the native_min_value to use for device if any."""
        return self._native_step

    @property
    def assumed_state(self):
        """Return if the state is based on assumptions."""
        return self._assumed

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._state

    async def event_handler(self, e):
        if self.uuidAction in e.data:
            data = e.data[self.uuidAction]
            if isinstance(data, (list, dict)):
                data = str(data)
                if len(data) >= 255:
                    self._state = data[:255]
                else:
                    self._state = data
            else:
                self._state = data

            self.schedule_update_ha_state()

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes.

        Implemented by platform classes.
        """
        return {
            **self._attr_extra_state_attributes,
            "state_uuid": self.states["value"],
            "device_type": self.type,
            "platform": "loxone",
        }

    async def async_set_native_value(self, value: float):
        """Set new value."""
        await self.async_send_command(self.uuidAction, f"{value}")
        self.async_schedule_update_ha_state()
