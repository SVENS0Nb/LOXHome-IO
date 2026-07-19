import ast
import logging
import math
from functools import cached_property

import homeassistant.util.color as color_util
from homeassistant.components.light import (ATTR_BRIGHTNESS,
                                            ATTR_COLOR_TEMP_KELVIN,
                                            ATTR_HS_COLOR, ColorMode,
                                            LightEntity)
from homeassistant.const import STATE_UNKNOWN

from .. import LoxoneEntity
from ..helpers import get_or_create_device, hass_to_lox, lox_to_hass

_LOGGER = logging.getLogger(__name__)


def _parse_color_value(value, prefix: str, length: int):
    """Parse a Loxone color tuple without executing input as Python code."""
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    try:
        parsed = ast.literal_eval(value[len(prefix) :])
    except (SyntaxError, ValueError):
        return None
    if (
        not isinstance(parsed, (tuple, list))
        or len(parsed) != length
        or not all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in parsed
        )
    ):
        return None
    return parsed


class TunableWhiteLight(LoxoneEntity, LightEntity):
    _attr_max_color_temp_kelvin = 6500
    _attr_min_color_temp_kelvin = 2000

    _attr_supported_color_modes: set[ColorMode] = {ColorMode.COLOR_TEMP}
    _attr_available = False
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        """Initialize the Tunable White Light."""
        self._attr_state = STATE_UNKNOWN
        self._attr_is_on = STATE_UNKNOWN
        self._attr_unique_id = self.uuidAction
        self._attr_color_mode = ColorMode.UNKNOWN
        self._color_uuid = kwargs.get("states", {}).get("color", None)

        self._async_add_devices = kwargs["async_add_devices"]
        self._light_controller_id = kwargs.get("lightcontroller_id", None)
        self._light_controller_name = kwargs.get("lightcontroller_name", None)

        self._name = self._attr_name
        if self._light_controller_name:
            self._attr_name = f"{self._light_controller_name}-{self._attr_name}"

        if self._light_controller_id:
            self.type = "LightControllerV2"
            self._attr_entity_registry_enabled_default = kwargs.get("enabled_default", True)
            self._attr_device_info = get_or_create_device(
                self._light_controller_id, self.name, self.type, self.room
            )
        else:
            self.type = "ColorPickerV2"
            self._attr_device_info = get_or_create_device(
                self._light_controller_id, self.name, self.type, self.room
            )

    @cached_property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._attr_unique_id

    @property
    def is_on(self) -> bool:
        return True if self._attr_brightness and self._attr_brightness > 0 else False

    async def async_turn_off(self) -> None:
        await self.async_send_command(self.uuidAction, "setBrightness/0")
        self.async_schedule_update_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._attr_color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            self._attr_brightness = kwargs.get(
                ATTR_BRIGHTNESS, self._attr_brightness or 255
            )
            await self.async_send_command(
                self.uuidAction,
                "temp({},{})".format(
                    hass_to_lox(self._attr_brightness), self._attr_color_temp_kelvin
                ),
            )
        elif ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            await self.async_send_command(
                self.uuidAction,
                f"setBrightness/{hass_to_lox(self._attr_brightness)}",
            )
        else:
            await self.async_send_command(self.uuidAction, "On")

    async def event_handler(self, e):
        request_update = False
        if self._color_uuid in e.data:
            _color = e.data[self._color_uuid]

            if (_color := _parse_color_value(_color, "temp", 2)) is not None:
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_color_temp_kelvin = _color[1]
                self._attr_brightness = round(255 * _color[0] / 100)
                request_update = True
            else:
                _LOGGER.error("Invalid Loxone tunable-white color state")

        if request_update:
            if not self._attr_available:
                self._attr_available = True
            self.async_schedule_update_ha_state()

    @cached_property
    def icon(self):
        """Return the sensor icon."""
        return "mdi:lightbulb"


class RGBColorPicker(LoxoneEntity, LightEntity):
    __color_mode_reported = True
    _attr_max_color_temp_kelvin = 6500
    _attr_min_color_temp_kelvin = 2000
    _attr_available = False
    
    _attr_supported_color_modes: set[ColorMode] = {
        ColorMode.COLOR_TEMP,
        ColorMode.HS,
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        """Initialize the LumiTech."""
        self._attr_unique_id = self.uuidAction
        self._attr_color_mode = ColorMode.UNKNOWN
        self._color_uuid = kwargs.get("states", {}).get("color", None)
        self._sequence_uuid = kwargs.get("states", {}).get("sequence", None)

        self._async_add_devices = kwargs["async_add_devices"]
        self._light_controller_id = kwargs.get("lightcontroller_id", None)
        self._light_controller_name = kwargs.get("lightcontroller_name", None)

        self._name = self._attr_name
        if self._light_controller_name:
            self._attr_name = f"{self._light_controller_name}-{self._attr_name}"

        if self._light_controller_id:
            self.type = "LightControllerV2"
            self._attr_entity_registry_enabled_default = kwargs.get("enabled_default", True)
            self._attr_device_info = get_or_create_device(
                self._light_controller_id, self.name, self.type, self.room
            )
        else:
            self.type = "ColorPickerV2"
            self._attr_device_info = get_or_create_device(
                self._light_controller_id, self.name, self.type, self.room
            )

    @cached_property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._attr_unique_id

    @property
    def is_on(self) -> bool:
        return True if self._attr_brightness and self._attr_brightness > 0 else False

    async def async_turn_off(self) -> None:
        await self.async_send_command(self.uuidAction, "setBrightness/0")
        self.async_schedule_update_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        if ATTR_HS_COLOR in kwargs:
            self._attr_brightness = kwargs.get(
                ATTR_BRIGHTNESS, self._attr_brightness or 255
            )
            r, g, b = color_util.color_hs_to_RGB(
                kwargs[ATTR_HS_COLOR][0], kwargs[ATTR_HS_COLOR][1]
            )
            h, s, _ = color_util.color_RGB_to_hsv(r, g, b)
            await self.async_send_command(
                self.uuidAction,
                "hsv({},{},{})".format(
                    h, s, hass_to_lox(self._attr_brightness)
                ),
            )
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._attr_color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            self._attr_brightness = kwargs.get(
                ATTR_BRIGHTNESS, self._attr_brightness or 255
            )
            await self.async_send_command(
                self.uuidAction,
                "temp({},{})".format(
                    hass_to_lox(self._attr_brightness), self._attr_color_temp_kelvin
                ),
            )

        elif ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            await self.async_send_command(
                self.uuidAction,
                f"setBrightness/{hass_to_lox(self._attr_brightness)}",
            )
        else:
            await self.async_send_command(self.uuidAction, "On")

    async def event_handler(self, e):
        request_update = False
        if self._color_uuid in e.data:
            _color = e.data[self._color_uuid]

            if (_parsed_color := _parse_color_value(_color, "hsv", 3)) is not None:
                _color = _parsed_color
                self._attr_color_mode = ColorMode.HS
                self._attr_hs_color = (_color[0], _color[1])
                self._attr_brightness = lox_to_hass(_color[2])
                request_update = True
            elif (_parsed_color := _parse_color_value(_color, "temp", 2)) is not None:
                _color = _parsed_color
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._attr_color_temp_kelvin = _color[1]
                self._attr_hs_color = None
                self._attr_brightness = round(255 * _color[0] / 100)
                request_update = True
            else:
                _LOGGER.error("Invalid Loxone RGB color state")

        if request_update:
            if not self._attr_available:
                self._attr_available = True
            self.async_schedule_update_ha_state()

    @cached_property
    def icon(self):
        """Return the sensor icon."""
        return "mdi:eyedropper-variant"


class LumiTech(RGBColorPicker):
    """Representation of a Loxone LumiTech Dimmer."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        """Initialize the LumiTech."""
        if self._light_controller_id:
            self.type = "LightControllerV2"
            self._attr_entity_registry_enabled_default = kwargs.get("enabled_default", True)
            self._attr_device_info = get_or_create_device(
                self._light_controller_id, self.name, self.type, self.room
            )
        else:
            self.type = "LumiTech"
            self._attr_device_info = get_or_create_device(
                self.unique_id, self.name, self.type, self.room
            )
