"""Provide info to system health."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register system health callbacks."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Return aggregate health data without private installation identifiers."""
    coordinators = list(hass.data.get(DOMAIN, {}).values())
    connected = 0
    software_versions: set[str] = set()
    miniserver_types: set[str] = set()
    for coordinator in coordinators:
        api = getattr(coordinator, "api", None)
        miniserver = getattr(coordinator, "miniserver", None)
        if api and getattr(api, "is_connected", False):
            connected += 1
        if miniserver:
            if version := getattr(miniserver, "software_version", None):
                software_versions.add(str(version))
            if (server_type := getattr(miniserver, "miniserver_type", None)) is not None:
                miniserver_types.add(str(server_type))

    return {
        "Configured Miniservers": len(coordinators),
        "Connected Miniservers": connected,
        "Loxone Software Versions": sorted(software_versions),
        "Miniserver Types": sorted(miniserver_types),
    }
