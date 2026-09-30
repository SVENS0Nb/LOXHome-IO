"""Strict, non-optimistic physical feedback decoding (no Home Assistant I/O)."""
from __future__ import annotations

import math
from typing import Any


def binary_feedback(value: Any, inverted: bool = False) -> bool | None:
    """Accept only actual binary values. Never turn NaN/unknown/2 into true."""
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("true", "on"):
            value = 1
        elif value in ("false", "off"):
            value = 0
        else:
            try:
                value = float(value)
            except ValueError:
                return None
    if not isinstance(value, (bool, int, float)) or value not in (0, 1):
        return None
    return bool(value) != inverted


def window_state(value: Any) -> int:
    """Preserve documented masks; invalid/fractional/unknown bits are unknown."""
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return 0
    if not math.isfinite(number) or not number.is_integer() or not 0 < number <= 31:
        return 0
    return int(number)
