"""Tests for the selective Loxone entity catalog."""

from types import SimpleNamespace

from custom_components.loxone.catalog import (
    action_selection_key,
    build_action_options,
    build_entity_catalog,
    control_selection_key,
    decode_action,
    door_selection_key,
    encode_action,
    entity_is_selected,
    sauna_selection_key,
    subcontrol_selection_key,
)
from custom_components.loxone.const import CONF_SELECTED_ENTITIES
from custom_components.loxone.helpers import add_room_and_cat_to_value_values


def _structure() -> dict:
    return {
        "rooms": {"room-1": {"name": "Wellness"}},
        "cats": {"cat-1": {"name": "Klima"}},
        "controls": {
            "temp": {
                "uuidAction": "temp",
                "name": "Raumtemperatur",
                "type": "InfoOnlyAnalog",
                "room": "room-1",
                "cat": "cat-1",
            },
            "light": {
                "uuidAction": "light",
                "name": "Licht",
                "type": "LightControllerV2",
                "room": "room-1",
                "subControls": {
                    "dimmer": {
                        "uuidAction": "dimmer",
                        "name": "Decke",
                        "type": "Dimmer",
                    }
                },
            },
            "sauna": {
                "uuidAction": "sauna",
                "name": "Sauna",
                "type": "Sauna",
                "room": "room-1",
                "details": {"hasVaporizer": True, "hasDoorSensor": True},
                "states": {
                    "active": "s-active",
                    "power": "s-power",
                    "tempActual": "s-temp",
                    "tempTarget": "s-target",
                    "fan": "s-fan",
                    "doorClosed": "s-door",
                    "mode": "s-mode",
                    "humidityActual": "s-humidity",
                    "humidityTarget": "s-humidity-target",
                    "lessWater": "s-water",
                    "timer": "s-timer",
                },
            },
            "monitor": {
                "uuidAction": "monitor",
                "name": "Fenster- und Türmonitor",
                "type": "WindowMonitor",
                "details": {
                    "windows": [
                        {
                            "uuid": "front-door",
                            "name": "Haustür",
                            "room": "room-1",
                        }
                    ]
                },
                "states": {"windowStates": "window-states"},
            },
            "nfc": {
                "uuidAction": "nfc",
                "name": "Code Touch",
                "type": "NfcCodeTouch",
                "room": "room-1",
                "details": {"accessOutputs": {"q1": "Haustür"}},
            },
            "meter": {
                "uuidAction": "meter",
                "name": "Energiezähler",
                "type": "Meter",
                "room": "room-1",
                "states": {"actual": "meter-actual", "total": "meter-total"},
            },
            "ventilation": {
                "uuidAction": "ventilation",
                "name": "Lüftung",
                "type": "Ventilation",
                "room": "room-1",
                "details": {"hasPresence": True, "hasIndoorHumidity": True},
                "states": {
                    "presence": "vent-presence",
                    "humidityIndoor": "vent-humidity",
                },
            },
            "intercom": {
                "uuidAction": "intercom",
                "name": "Sprechanlage",
                "type": "Intercom",
                "room": "room-1",
                "subControls": {
                    "bell": {
                        "uuidAction": "intercom-bell",
                        "name": "Türöffner",
                        "type": "Switch",
                    }
                },
            },
        },
    }


def test_catalog_contains_individual_complex_capabilities() -> None:
    catalog = {candidate.key: candidate for candidate in build_entity_catalog(_structure())}

    assert control_selection_key("temp", "sensor") in catalog
    assert subcontrol_selection_key("dimmer", "light") in catalog
    assert sauna_selection_key("sauna", "climate") in catalog
    assert sauna_selection_key("sauna", "humidity_target") in catalog
    assert door_selection_key("monitor", "front-door") in catalog
    assert action_selection_key("nfc", "output/1") in catalog
    assert subcontrol_selection_key("meter-actual", "sensor") in catalog
    assert subcontrol_selection_key("meter-total", "sensor") in catalog
    assert subcontrol_selection_key("vent-presence", "binary_sensor") in catalog
    assert subcontrol_selection_key("vent-humidity", "sensor") in catalog
    assert subcontrol_selection_key("intercom-bell", "switch") in catalog
    assert control_selection_key("meter", "sensor") not in catalog
    assert control_selection_key("intercom", "switch") not in catalog
    assert "Wellness" in catalog[door_selection_key("monitor", "front-door")].label


def test_catalog_handles_textual_capability_flags() -> None:
    structure = _structure()
    sauna = structure["controls"]["sauna"]
    sauna["details"]["hasVaporizer"] = "0"
    ventilation = structure["controls"]["ventilation"]
    ventilation["details"]["hasPresence"] = "false"
    ventilation["details"]["hasIndoorHumidity"] = "0"
    ventilation["details"]["hasIndorHumidity"] = "1"

    keys = {candidate.key for candidate in build_entity_catalog(structure)}

    assert sauna_selection_key("sauna", "mode") not in keys
    assert sauna_selection_key("sauna", "humidity_target") not in keys
    assert subcontrol_selection_key("vent-presence", "binary_sensor") not in keys
    assert subcontrol_selection_key("vent-humidity", "sensor") in keys


def test_old_entries_import_all_but_new_entries_only_import_selection() -> None:
    legacy_entry = SimpleNamespace(options={})
    selected_entry = SimpleNamespace(options={CONF_SELECTED_ENTITIES: ["control:wanted:sensor"]})

    assert entity_is_selected(legacy_entry, "anything") is True
    assert entity_is_selected(legacy_entry, "action:nfc:output/1") is False
    assert entity_is_selected(legacy_entry, "door:monitor:front-door") is False
    assert entity_is_selected(legacy_entry, "sauna:sauna:climate") is False
    assert entity_is_selected(selected_entry, "control:wanted:sensor") is True
    assert entity_is_selected(selected_entry, "control:other:sensor") is False


def test_window_monitor_selection_survives_reordering() -> None:
    structure = _structure()
    windows = structure["controls"]["monitor"]["details"]["windows"]
    windows.append({"uuid": "back-door", "name": "Terrassentür"})
    first_keys = {
        candidate.key
        for candidate in build_entity_catalog(structure)
        if candidate.platform == "lock"
    }

    windows.reverse()
    reordered_keys = {
        candidate.key
        for candidate in build_entity_catalog(structure)
        if candidate.platform == "lock"
    }

    assert reordered_keys == first_keys


def test_duplicate_legacy_doors_still_get_distinct_selection_keys() -> None:
    structure = _structure()
    windows = structure["controls"]["monitor"]["details"]["windows"]
    windows[0].pop("uuid")
    windows.append(dict(windows[0]))

    lock_keys = [
        candidate.key
        for candidate in build_entity_catalog(structure)
        if candidate.platform == "lock"
    ]

    assert len(lock_keys) == 2
    assert len(set(lock_keys)) == 2


def test_door_action_options_are_encoded_without_guessing_relationships() -> None:
    actions = build_action_options(_structure())
    nfc_action = next(item for item in actions if "Haustür" in item["label"])

    assert nfc_action["value"] == encode_action("nfc", "output/1")
    assert decode_action(nfc_action["value"]) == ("nfc", "output/1")
    assert decode_action(None) is None


def test_room_resolution_does_not_mutate_shared_structure() -> None:
    structure = _structure()
    sauna = structure["controls"]["sauna"]

    resolved = add_room_and_cat_to_value_values(structure, sauna)

    assert resolved["room"] == "Wellness"
    assert sauna["room"] == "room-1"
