"""Build the selectable Home Assistant entity catalog from LoxAPP3.json."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final

from .const import CONF_SELECTED_ENTITIES


@dataclass(frozen=True, slots=True)
class EntityCandidate:
    """A single Home Assistant entity that can be selected during setup."""

    key: str
    platform: str
    name: str
    source_type: str
    source_uuid: str
    room: str = ""
    category: str = ""

    @property
    def label(self) -> str:
        """Return a compact, unique label for the Home Assistant selector."""
        location = " / ".join(part for part in (self.room, self.category) if part)
        prefix = f"{location} · " if location else ""
        return f"{prefix}{self.name} [{self.platform} · {self.source_type}]"


CONTROL_PLATFORMS: Final[dict[str, tuple[str, ...]]] = {
    "AcControl": ("climate",),
    "Alarm": ("alarm_control_panel",),
    "AudioZoneV2": ("media_player",),
    "Dimmer": ("light",),
    "EIBDimmer": ("light",),
    "Gate": ("cover",),
    "IRoomController": ("climate",),
    "IRoomControllerV2": ("climate",),
    "InfoOnlyAnalog": ("sensor",),
    "InfoOnlyDigital": ("binary_sensor",),
    "Jalousie": ("cover",),
    "LightControllerV2": ("light",),
    "PresenceDetector": ("binary_sensor",),
    "Pushbutton": ("button",),
    "Radio": ("select",),
    "Slider": ("number",),
    "SmokeAlarm": ("binary_sensor",),
    "Switch": ("switch",),
    "TextInput": ("text", "sensor"),
    "TimedSwitch": ("switch",),
    "Ventilation": ("fan",),
    "Window": ("cover",),
}

SAUNA_ENTITIES: Final[tuple[tuple[str, str, str], ...]] = (
    ("climate", "climate", "Saunasteuerung"),
    ("mode", "select", "Betriebsart"),
    ("fan", "fan", "Lüftung"),
    ("temp_actual", "sensor", "Temperatur"),
    ("temp_bench", "sensor", "Banktemperatur"),
    ("humidity_actual", "sensor", "Luftfeuchtigkeit"),
    ("humidity_target", "number", "Zielfeuchtigkeit"),
    ("timer", "sensor", "Restlaufzeit"),
    ("heating", "binary_sensor", "Heizung"),
    ("drying", "binary_sensor", "Trocknung"),
    ("door", "binary_sensor", "Tür"),
    ("presence", "binary_sensor", "Präsenz"),
    ("error", "binary_sensor", "Störung"),
    ("low_water", "binary_sensor", "Wassermangel"),
    ("start_timer", "button", "Timer starten"),
)


def _lookup_name(structure: dict[str, Any], section: str, uuid: str) -> str:
    return str(structure.get(section, {}).get(uuid, {}).get("name", ""))


def feature_is_enabled(value: Any) -> bool:
    """Interpret Loxone capability flags without treating textual zero as true."""
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"", "0", "0.0", "false", "off", "no"}:
            return False
        if normalized in {"1", "1.0", "true", "on", "yes"}:
            return True
    return bool(value)


def ventilation_capability_is_enabled(
    details: dict[str, Any], capability: str | None
) -> bool:
    """Read Ventilation flags, including Loxone's historic spelling."""
    if capability is None:
        return True
    keys = (capability,)
    if capability == "hasIndoorHumidity":
        keys = ("hasIndoorHumidity", "hasIndorHumidity")
    return any(feature_is_enabled(details.get(key)) for key in keys)


def control_selection_key(control_or_uuid: dict[str, Any] | str, platform: str) -> str:
    """Return the stable selection key for a top-level Loxone control."""
    uuid = control_or_uuid.get("uuidAction", "") if isinstance(control_or_uuid, dict) else control_or_uuid
    return f"control:{uuid}:{platform}"


def subcontrol_selection_key(uuid: str, platform: str) -> str:
    """Return the stable selection key for a subcontrol."""
    return f"subcontrol:{uuid}:{platform}"


def sauna_selection_key(uuid: str, suffix: str) -> str:
    """Return the stable selection key for a Sauna capability."""
    return f"sauna:{uuid}:{suffix}"


def door_identity(window: Any, index: int) -> str:
    """Return the stable WindowMonitor child identity, with a legacy fallback."""
    if isinstance(window, dict) and window.get("uuid"):
        return str(window["uuid"])
    if isinstance(window, dict):
        fingerprint_parts = (
            str(window.get("name", "")).strip(),
            str(window.get("room", "")).strip(),
            str(window.get("installPlace", "")).strip(),
        )
        if any(fingerprint_parts):
            digest = hashlib.sha256("\x1f".join(fingerprint_parts).encode()).hexdigest()
            return f"legacy-{digest[:16]}"
    return f"index-{index}"


def door_identities(windows: Any) -> list[str]:
    """Return unique child IDs while keeping the first stable on duplicates."""
    result: list[str] = []
    occurrences: dict[str, int] = {}
    for index, item in enumerate(windows or ()):
        window = item if isinstance(item, dict) else {"name": str(item)}
        base_identity = door_identity(window, index)
        occurrence = occurrences.get(base_identity, 0) + 1
        occurrences[base_identity] = occurrence
        result.append(
            base_identity
            if occurrence == 1
            else f"{base_identity}-duplicate-{occurrence}"
        )
    return result


def door_selection_key(monitor_uuid: str, door_id: str | int) -> str:
    """Return the stable selection key for a WindowMonitor door."""
    return f"door:{monitor_uuid}:{door_id}"


def system_selection_key(suffix: str) -> str:
    """Return the selection key for integration-level diagnostic sensors."""
    return f"system:{suffix}"


def entity_is_selected(config_entry: Any, key: str, *fallback_keys: str) -> bool:
    """Return whether an entity was selected.

    Config entries created before selective import keep their historic entities,
    but newly introduced physical actions remain opt-in.
    """
    options = getattr(config_entry, "options", {}) or {}
    if CONF_SELECTED_ENTITIES not in options:
        return not key.startswith(("action:", "door:", "access_lock:", "sauna:"))
    configured = options.get(CONF_SELECTED_ENTITIES, ())
    if not isinstance(configured, (list, tuple, set)):
        return False
    selected = {item for item in configured if isinstance(item, str)}
    return key in selected or any(item in selected for item in fallback_keys)


def control_is_selected(config_entry: Any, control: dict[str, Any], platform: str) -> bool:
    """Return whether a top-level Loxone control should be created."""
    return entity_is_selected(config_entry, control_selection_key(control, platform))


def build_entity_catalog(structure: dict[str, Any]) -> list[EntityCandidate]:
    """Create all selectable entity candidates from a Loxone structure file."""
    candidates: list[EntityCandidate] = [
        EntityCandidate(
            key=system_selection_key("keepalive"),
            platform="sensor",
            name="Letzte Keepalive-Nachricht",
            source_type="Miniserver",
            source_uuid="system",
        ),
        EntityCandidate(
            key=system_selection_key("version"),
            platform="sensor",
            name="Softwareversion",
            source_type="Miniserver",
            source_uuid="system",
        ),
    ]

    controls = structure.get("controls", {}) or {}
    for fallback_uuid, control in controls.items():
        uuid = str(control.get("uuidAction") or fallback_uuid)
        control_type = str(control.get("type", "Unknown"))
        name = str(control.get("name") or control_type)
        room = _lookup_name(structure, "rooms", str(control.get("room", "")))
        category = _lookup_name(structure, "cats", str(control.get("cat", "")))

        for platform in CONTROL_PLATFORMS.get(control_type, ()):
            candidates.append(
                EntityCandidate(
                    key=control_selection_key(uuid, platform),
                    platform=platform,
                    name=name,
                    source_type=control_type,
                    source_uuid=uuid,
                    room=room,
                    category=category,
                )
            )

        # A pushbutton is not automatically a door. This separate, opt-in
        # candidate lets the user explicitly identify a door-release button.
        if control_type == "Pushbutton":
            candidates.append(EntityCandidate(
                key=access_lock_selection_key(uuid, "pulse"), platform="lock",
                name=name, source_type="Pushbutton (door profile)",
                source_uuid=uuid, room=room, category=category,
            ))

        if control_type == "LightControllerV2":
            for sub_uuid, subcontrol in (control.get("subControls", {}) or {}).items():
                if subcontrol.get("type") not in {"Switch", "Dimmer", "ColorPickerV2"}:
                    continue
                candidates.append(
                    EntityCandidate(
                        key=subcontrol_selection_key(str(subcontrol.get("uuidAction") or sub_uuid), "light"),
                        platform="light",
                        name=f"{name} · {subcontrol.get('name', subcontrol.get('type', 'Ausgang'))}",
                        source_type=str(subcontrol.get("type", "Subcontrol")),
                        source_uuid=str(subcontrol.get("uuidAction") or sub_uuid),
                        room=room,
                        category=category,
                    )
                )

        if control_type == "Meter":
            for state_key, state_name in (
                ("actual", "Aktueller Wert"),
                ("total", "Gesamtwert"),
                ("totalNeg", "Negativer Gesamtwert"),
                ("storage", "Füllstand"),
            ):
                state_uuid = str((control.get("states", {}) or {}).get(state_key, ""))
                if not state_uuid:
                    continue
                candidates.append(
                    EntityCandidate(
                        key=subcontrol_selection_key(state_uuid, "sensor"),
                        platform="sensor",
                        name=f"{name} · {state_name}",
                        source_type=f"Meter/{state_key}",
                        source_uuid=state_uuid,
                        room=room,
                        category=category,
                    )
                )

        if control_type == "Ventilation":
            ventilation_states = control.get("states", {}) or {}
            ventilation_details = control.get("details", {}) or {}
            for state_key, platform, state_name, capability in (
                ("presence", "binary_sensor", "Präsenz", "hasPresence"),
                ("humidityIndoor", "sensor", "Luftfeuchtigkeit", "hasIndoorHumidity"),
                ("airQualityIndoor", "sensor", "Luftqualität", None),
                ("temperatureOutdoor", "sensor", "Außentemperatur", None),
            ):
                state_uuid = str(ventilation_states.get(state_key, ""))
                if not state_uuid or (
                    not ventilation_capability_is_enabled(
                        ventilation_details, capability
                    )
                ):
                    continue
                candidates.append(
                    EntityCandidate(
                        key=subcontrol_selection_key(state_uuid, platform),
                        platform=platform,
                        name=f"{name} · {state_name}",
                        source_type=f"Ventilation/{state_key}",
                        source_uuid=state_uuid,
                        room=room,
                        category=category,
                    )
                )

        if control_type == "Intercom":
            for sub_uuid, subcontrol in (control.get("subControls", {}) or {}).items():
                action_uuid = str(subcontrol.get("uuidAction") or sub_uuid)
                candidates.append(
                    EntityCandidate(
                        key=subcontrol_selection_key(action_uuid, "switch"),
                        platform="switch",
                        name=f"{name} · {subcontrol.get('name', 'Ausgang')}",
                        source_type="Intercom/Subcontrol",
                        source_uuid=action_uuid,
                        room=room,
                        category=category,
                    )
                )

        if control_type == "Sauna":
            states = control.get("states", {}) or {}
            details = control.get("details", {}) or {}
            for suffix, platform, display_name in SAUNA_ENTITIES:
                if not _sauna_capability_available(suffix, states, details):
                    continue
                candidates.append(
                    EntityCandidate(
                        key=sauna_selection_key(uuid, suffix),
                        platform=platform,
                        name=f"{name} · {display_name}",
                        source_type="Sauna",
                        source_uuid=uuid,
                        room=room,
                        category=category,
                    )
                )

        if control_type == "WindowMonitor":
            windows = control.get("details", {}).get("windows", ()) or ()
            for index, (window, door_id) in enumerate(
                zip(windows, door_identities(windows), strict=False)
            ):
                window_data = window if isinstance(window, dict) else {"name": str(window)}
                candidates.append(
                    EntityCandidate(
                        key=door_selection_key(uuid, door_id),
                        platform="lock",
                        name=str(window_data.get("name") or f"{name} {index + 1}"),
                        source_type="WindowMonitor",
                        source_uuid=uuid,
                        room=_lookup_name(structure, "rooms", str(window_data.get("room", ""))) or room,
                        category=category,
                    )
                )

        if control_type in {"NfcCodeTouch", "NFCCodeTouch", "NFC Code Touch"}:
            for output_key, output_name in (control.get("details", {}).get("accessOutputs", {}) or {}).items():
                output_number = str(output_key).lower().removeprefix("q")
                candidates.append(
                    EntityCandidate(
                        key=action_selection_key(uuid, f"output/{output_number}"),
                        platform="button",
                        name=f"{name} · {output_name}",
                        source_type="NFC Code Touch",
                        source_uuid=uuid,
                        room=room,
                        category=category,
                    )
                )
                candidates.append(EntityCandidate(
                    key=access_lock_selection_key(uuid, f"output/{output_number}"),
                    platform="lock", name=f"{name} · {output_name}",
                    source_type="NFC Code Touch (door profile)", source_uuid=uuid,
                    room=room, category=category,
                ))

    candidates.sort(key=lambda item: (item.room.casefold(), item.platform, item.name.casefold(), item.key))
    return candidates


def _sauna_capability_available(suffix: str, states: dict[str, Any], details: dict[str, Any]) -> bool:
    if suffix == "climate":
        return all(state in states for state in ("active", "power", "tempActual", "tempTarget"))
    if suffix == "start_timer":
        return "timer" in states
    required_state = {
        "door": "doorClosed",
        "drying": "drying",
        "error": "error",
        "fan": "fan",
        "heating": "power",
        "humidity_actual": "humidityActual",
        "humidity_target": "humidityTarget",
        "low_water": "lessWater",
        "mode": "mode",
        "presence": "presence",
        "temp_actual": "tempActual",
        "temp_bench": "tempBench",
        "timer": "timer",
    }.get(suffix)
    if required_state and required_state not in states:
        return False
    if suffix in {"humidity_actual", "humidity_target", "low_water", "mode"}:
        return feature_is_enabled(details.get("hasVaporizer"))
    if suffix == "door":
        return feature_is_enabled(details.get("hasDoorSensor"))
    return True


def action_selection_key(uuid: str, command: str) -> str:
    """Return the key used when an action is exposed as a button."""
    return f"action:{uuid}:{command}"


def access_lock_selection_key(uuid: str, command: str) -> str:
    """A distinct opt-in identity; never migrate an existing button implicitly."""
    return f"access_lock:{uuid}:{command}"


def build_feedback_options(structure: dict[str, Any]) -> list[dict[str, str]]:
    """Only explicitly exported digital inputs, never command/security flags."""
    result = []
    for control in (structure.get("controls", {}) or {}).values():
        state = (control.get("states", {}) or {}).get("active")
        if control.get("type") != "InfoOnlyDigital" or not isinstance(state, str) or not state:
            continue
        room = _lookup_name(structure, "rooms", str(control.get("room", "")))
        result.append({"value": state, "label": f"{room} · {control.get('name', 'Digital input')}"})
    return sorted(result, key=lambda item: (item["label"].casefold(), item["value"]))


def action_is_secured(structure: dict[str, Any], uuid: str) -> bool:
    """Read the source control flag, not its name or a cached command result."""
    return any(
        str(control.get("uuidAction") or fallback) == uuid
        and feature_is_enabled(control.get("isSecured"))
        for fallback, control in (structure.get("controls", {}) or {}).items()
    )


def encode_action(uuid: str, command: str) -> str:
    """Encode an action for storage in ConfigEntry options."""
    return f"{uuid}|{command}"


def decode_action(value: str | None) -> tuple[str, str] | None:
    """Decode a stored action reference."""
    if not value or "|" not in value:
        return None
    uuid, command = value.split("|", 1)
    return (uuid, command) if uuid and command else None


def build_action_options(structure: dict[str, Any]) -> list[dict[str, str]]:
    """Build action selector options suitable for door profiles."""
    options: list[dict[str, str]] = []
    for fallback_uuid, control in (structure.get("controls", {}) or {}).items():
        uuid = str(control.get("uuidAction") or fallback_uuid)
        name = str(control.get("name") or control.get("type") or uuid)
        control_type = str(control.get("type", ""))
        room = _lookup_name(structure, "rooms", str(control.get("room", "")))
        prefix = f"{room} · " if room else ""
        if control_type == "Pushbutton":
            options.append({"value": encode_action(uuid, "pulse"), "label": f"{prefix}{name} · Impuls"})
        elif control_type in {"Switch", "TimedSwitch"}:
            options.extend(
                (
                    {"value": encode_action(uuid, "on"), "label": f"{prefix}{name} · Ein"},
                    {"value": encode_action(uuid, "off"), "label": f"{prefix}{name} · Aus"},
                    {"value": encode_action(uuid, "pulse"), "label": f"{prefix}{name} · Impuls"},
                )
            )
        elif control_type in {"NfcCodeTouch", "NFCCodeTouch", "NFC Code Touch"}:
            for output_key, output_name in (control.get("details", {}).get("accessOutputs", {}) or {}).items():
                output_number = str(output_key).lower().removeprefix("q")
                options.append(
                    {
                        "value": encode_action(uuid, f"output/{output_number}"),
                        "label": f"{prefix}{name} · {output_name}",
                    }
                )
    return sorted(options, key=lambda item: item["label"].casefold())
