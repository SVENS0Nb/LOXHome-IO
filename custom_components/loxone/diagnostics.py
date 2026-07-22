"""Privacy-preserving diagnostics for LOXHome I/O."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ALLOW_INSECURE_HTTP,
    CONF_DOOR_PROFILES,
    CONF_SELECTED_ENTITIES,
    CONF_VERIFY_SSL,
    DOMAIN,
)


def _structure_summary(structure: dict[str, Any]) -> dict[str, Any]:
    """Summarize a structure without exposing names, UUIDs, or serial numbers."""
    control_types: Counter[str] = Counter()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            control_type = value.get("type")
            if isinstance(control_type, str) and value.get("uuidAction"):
                control_types[control_type] += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    controls = structure.get("controls", {})
    visit(controls)
    rooms = structure.get("rooms", {})
    categories = structure.get("cats", {})
    return {
        "control_count": sum(control_types.values()),
        "control_types": dict(sorted(control_types.items())),
        "room_count": len(rooms) if isinstance(rooms, dict) else 0,
        "category_count": len(categories) if isinstance(categories, dict) else 0,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return entry-scoped diagnostics without private Loxone structure data."""
    coordinator = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if coordinator is None:
        return {"loaded": False}

    api = getattr(coordinator, "api", None)
    miniserver = getattr(coordinator, "miniserver", None)
    structure = getattr(api, "structure_file", {})
    if not isinstance(structure, dict):
        structure = {}

    selected = config_entry.options.get(CONF_SELECTED_ENTITIES, ())
    profiles = config_entry.options.get(CONF_DOOR_PROFILES, {})
    return {
        "loaded": True,
        "connected": bool(api and api.is_connected),
        "transport": {
            "tls": getattr(api, "scheme", "http") == "https",
            "verify_ssl": bool(config_entry.options.get(CONF_VERIFY_SSL, True)),
            "insecure_http_allowed": bool(
                config_entry.options.get(CONF_ALLOW_INSECURE_HTTP, False)
            ),
        },
        "selection": {
            "entity_count": len(selected) if isinstance(selected, (list, tuple)) else 0,
            "door_profile_count": len(profiles) if isinstance(profiles, dict) else 0,
        },
        "miniserver": {
            "type": getattr(miniserver, "miniserver_type", None),
            "software_version": getattr(miniserver, "software_version", None),
        },
        "structure": _structure_summary(structure),
    }
