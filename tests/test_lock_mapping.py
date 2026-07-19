"""Tests for physical door state parsing."""

from custom_components.loxone.catalog import door_identity
from custom_components.loxone.lock import parse_window_states


def test_parse_window_monitor_text_states() -> None:
    assert parse_window_states("8,16,5") == [8, 16, 5]
    assert parse_window_states("[8, 16, 5]") == [8, 16, 5]


def test_parse_window_monitor_tolerates_bad_and_list_values() -> None:
    assert parse_window_states("8,invalid,16") == [8, 0, 16]
    assert parse_window_states([8.0, "16"]) == [8, 16]
    assert parse_window_states([float("inf"), -8]) == [0, 0]
    assert parse_window_states(None) == []


def test_door_identity_prefers_child_uuid_and_has_explicit_fallback() -> None:
    assert door_identity({"uuid": "door-uuid"}, 4) == "door-uuid"
    assert door_identity({"name": "Legacy"}, 4).startswith("legacy-")
    assert door_identity({}, 4) == "index-4"
