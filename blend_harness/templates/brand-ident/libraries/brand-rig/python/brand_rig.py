"""Small reusable helpers for deterministic brand scene parameters."""

from __future__ import annotations

from collections.abc import Sequence


def rgba(value: Sequence[float]) -> tuple[float, float, float, float]:
    """Return one bounded four-channel color tuple."""
    if len(value) != 4:
        raise ValueError("RGBA colors require exactly four channels")
    channels = tuple(float(channel) for channel in value)
    if any(channel < 0.0 or channel > 1.0 for channel in channels):
        raise ValueError("RGBA channels must remain inside 0..1")
    return channels
