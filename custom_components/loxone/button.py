"""
Loxone Buttons

For more details about this component, please refer to the documentation at
https://github.com/JoDehli/PyLoxone
"""

import logging
from functools import cached_property
from typing import final

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from . import LoxoneEntity
from .catalog import (
    action_selection_key,
    control_is_selected,
    entity_is_selected,
    sauna_selection_key,
)
from .const import DOMAIN
from .helpers import add_room_and_cat_to_value_values, get_all
from .miniserver import get_miniserver_from_hass

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Loxone Button."""
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

    for button_entity in get_all(loxconfig, ["Pushbutton"]):
        if not control_is_selected(config_entry, button_entity, "button"):
            continue
        button_entity = add_room_and_cat_to_value_values(loxconfig, button_entity)
        entities.append(LoxoneButton(**button_entity))

    for action_control in get_all(
        loxconfig, ["NfcCodeTouch", "NFCCodeTouch", "NFC Code Touch"]
    ):
        action_control = add_room_and_cat_to_value_values(loxconfig, action_control)
        action_control["config_entry_id"] = config_entry.entry_id
        action_control["gateway_id"] = config_entry.unique_id or config_entry.entry_id
        for output_key, output_name in (
            action_control.get("details", {}).get("accessOutputs", {}) or {}
        ).items():
            output_number = str(output_key).lower().removeprefix("q")
            command = f"output/{output_number}"
            if not entity_is_selected(
                config_entry,
                action_selection_key(action_control["uuidAction"], command),
            ):
                continue
            action_entity = {
                **action_control,
                "name": f"{action_control['name']} {output_name}",
            }
            entities.append(
                LoxoneActionButton(
                    **action_entity,
                    command=command,
                )
            )

    from .sauna import LoxoneSaunaTimerButton

    for sauna in get_all(loxconfig, "Sauna"):
        if not entity_is_selected(
            config_entry, sauna_selection_key(sauna["uuidAction"], "start_timer")
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
        entities.append(LoxoneSaunaTimerButton(**sauna))

    async_add_entities(entities)


class LoxoneActionButton(LoxoneEntity, ButtonEntity):
    """A named Loxone command exposed as a Home Assistant button."""

    def __init__(self, *, command: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._command = command
        self._source_uuid = self.uuidAction

    @property
    def unique_id(self) -> str:
        return f"{self._gateway_id}-{self._source_uuid}-{self._command.replace('/', '-')}"

    async def async_press(self) -> None:
        await self.async_send_command(self._source_uuid, self._command)


class LoxoneButton(LoxoneEntity, ButtonEntity):
    """Representation of a Loxone pushbutton."""

    __last_pressed_isoformat: str | None = None
    _attr_unique_id: str | None = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._attr_icon = None
        self._attr_unique_id = self.uuidAction
        self._attr_state = None
        self._state_value = None

    @property
    def icon(self):
        """Return the icon to use for device if any."""
        return self._attr_icon

    # noinspection PyFinal
    @cached_property
    @final
    def state(self) -> str | None:
        """Return the entity state."""
        return self.__last_pressed_isoformat

    def __set_state(self, state: str | None) -> None:
        """Set the entity state."""
        # Invalidate the cache of the cached property
        self.__dict__.pop("state", None)
        self.__last_pressed_isoformat = state

    async def event_handler(self, event):
        request_update = False
        if "active" in self.states:
            if self.states["active"] in event.data:
                active = event.data[self.states["active"]]
                new_state = True if active == 1.0 else False
                if new_state != self._attr_state:
                    self._attr_state = new_state
                    self._state_value = active
                    self.__set_state(dt_util.utcnow().isoformat())
                    request_update = True
        if request_update:
            self.async_schedule_update_ha_state()

    @cached_property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._attr_unique_id

    async def async_press(self, **kwargs):
        """Press the button."""
        await self.async_send_command(self.uuidAction, "pulse")
        self.schedule_update_ha_state()

    @property
    def extra_state_attributes(self):
        """Return device specific state attributes."""
        return {
            **self._attr_extra_state_attributes,
            "state_uuid": self.states["active"],
            "new_state": self._attr_state,
            "state_value": self._state_value,
            "device_type": self.type,
        }

    @property
    def device_info(self):
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            name=self.name,
            manufacturer="Loxone",
            model=self.type,
            suggested_area=self.room,
        )
