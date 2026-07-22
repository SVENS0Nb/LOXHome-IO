import asyncio
import logging
import traceback
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

from homeassistant.const import (CONF_HOST, CONF_PASSWORD, CONF_PORT,
                                 CONF_USERNAME)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr

# from .api import LoxApp, LoxWs
from .helpers import get_miniserver_type

_LOGGER = logging.getLogger(__name__)
CONNECTION_NETWORK_MAC = "mac"
DOMAIN = "loxone"
NEW_GROUP = "groups"
NEW_LIGHT = "lights"
NEW_SCENE = "scenes"
NEW_SENSOR = "sensors"
NEW_COVERS = "covers"


def _configuration_url(host: str, port: int) -> str:
    """Return a credential-free URL matching the configured transport."""
    parsed = urlparse(host if "://" in host else f"//{host}")
    scheme = parsed.scheme.casefold() or ("https" if port == 443 else "http")
    hostname = parsed.hostname or parsed.path
    if not hostname:
        raise ValueError("A Miniserver hostname is required")
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    effective_port = parsed.port or port
    default_port = 443 if scheme == "https" else 80
    netloc = (
        f"{display_host}:{effective_port}"
        if effective_port != default_port
        else display_host
    )
    return urlunparse((scheme, netloc, parsed.path.rstrip("/"), "", "", ""))


@callback
def get_miniserver_from_hass(hass, config_entry):
    """Return the Miniserver for this specific config entry."""
    return hass.data[DOMAIN][config_entry.entry_id].miniserver


@dataclass
class ConfigDataClass:
    json: Optional[Dict[str, Any]] = None

    def get(self, key, default=None):
        if self.json is not None:
            return self.json.get(key, default)
        return default

    def __contains__(self, key):
        if self.json is not None:
            return key in self.json
        return False

    def __getitem__(self, key):
        if self.json is not None:
            return self.json[key]
        raise KeyError(key)


class MiniServer:
    def __init__(self, hass, lox_config, config_entry):
        self.hass = hass
        self.lox_config: ConfigDataClass = ConfigDataClass(lox_config)
        self.config_entry = config_entry
        self.listeners = []

    @property
    def serial(self):
        return self.lox_config.get("msInfo", {}).get("serialNr", None)

    @property
    def miniserver_type(self):
        return self.lox_config.get("msInfo", {}).get("miniserverType", None)

    @property
    def name(self):
        return self.lox_config.get("msInfo", {}).get("msName", None)

    @property
    def software_version(self):
        return ".".join([str(x) for x in self.lox_config.get("softwareVersion", "")])

    @property
    def miniserver_id(self) -> str:
        """Return the unique identifier of the Miniserver."""
        return self.config_entry.unique_id

    @callback
    def async_signal_new_device(self, device_type) -> str:
        """Gateway specific event to signal new device."""
        new_device = {
            NEW_GROUP: f"loxone_new_group_{self.miniserver_id}",
            NEW_LIGHT: f"loxone_new_light_{self.miniserver_id}",
            NEW_SCENE: f"loxone_new_scene_{self.miniserver_id}",
            NEW_SENSOR: f"loxone_new_sensor_{self.miniserver_id}",
            NEW_COVERS: f"loxone_new_cover_{self.miniserver_id}",
        }
        return new_device[device_type]

    async def async_update_device_registry(self) -> None:
        device_registry = dr.async_get(self.hass)
        # Host device
        # device_registry.async_get_or_create(
        #     config_entry_id=self.config_entry.entry_id,
        #     connections={
        #         (CONNECTION_NETWORK_MAC, self.config_entry.options[CONF_HOST])
        #     },
        # )

        # Miniserver service
        device_registry.async_get_or_create(
            config_entry_id=self.config_entry.entry_id,
            connections={
                (CONNECTION_NETWORK_MAC, self.config_entry.options[CONF_HOST])
            },
            name=self.name,
            model=get_miniserver_type(self.miniserver_type),
            identifiers={(DOMAIN, self.serial)},
            manufacturer="Loxone",
            sw_version=self.software_version,
            configuration_url=_configuration_url(
                str(self.config_entry.options[CONF_HOST]),
                int(self.config_entry.options[CONF_PORT]),
            ),
        )
