"""Regression tests for selectively imported Loxone buttons."""

import asyncio
from types import SimpleNamespace

import custom_components.loxone.button as button_platform
from custom_components.loxone.catalog import (
    action_selection_key,
    control_selection_key,
)
from custom_components.loxone.const import CONF_SELECTED_ENTITIES


def test_pushbutton_and_nfc_output_are_created_together(monkeypatch) -> None:
    """An NFC output must not abort setup of the entire button platform."""
    structure = {
        "rooms": {"room": {"name": "Apartment"}},
        "cats": {"access": {"name": "Access"}},
        "controls": {
            "push": {
                "uuidAction": "push-uuid",
                "name": "Emergency Exit",
                "type": "Pushbutton",
                "room": "room",
                "cat": "access",
                "states": {"active": "push-active"},
            },
            "nfc": {
                "uuidAction": "nfc-uuid",
                "name": "Door 2",
                "type": "NfcCodeTouch",
                "room": "room",
                "cat": "access",
                "details": {"accessOutputs": {"q2": "Door 2 Aktor 2"}},
            },
        },
    }
    config_entry = SimpleNamespace(
        entry_id="entry-id",
        unique_id="gateway-id",
        options={
            CONF_SELECTED_ENTITIES: [
                control_selection_key("push-uuid", "button"),
                action_selection_key("nfc-uuid", "output/2"),
            ]
        },
    )
    miniserver = SimpleNamespace(lox_config=SimpleNamespace(json=structure))
    monkeypatch.setattr(
        button_platform,
        "get_miniserver_from_hass",
        lambda _hass, _config_entry: miniserver,
    )
    created = []

    asyncio.run(
        button_platform.async_setup_entry(
            SimpleNamespace(),
            config_entry,
            lambda entities: created.extend(entities),
        )
    )

    assert [type(entity).__name__ for entity in created] == [
        "LoxoneButton",
        "LoxoneActionButton",
    ]
    assert [entity.name for entity in created] == [
        "Emergency Exit",
        "Door 2 Door 2 Aktor 2",
    ]
    assert created[1].unique_id == "gateway-id-nfc-uuid-output-2"
