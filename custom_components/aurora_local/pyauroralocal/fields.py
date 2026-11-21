"""Meaning of the heat pump's fields (units, scaling, derived values).

Based on docs/adlar-castra-protocol.md: meanings confirmed against the
vendor app and display, counters checked against integrated values."""

from __future__ import annotations

import math
from typing import Any

from .state import to_signed16

# instantpower values >= this are markers (32771, 32822 seen at start-up and
# during fault restarts), not real readings.
POWER_INVALID_FROM = 32768
QUIET_MODES = {0: "off", 1: "on", 2: "level_2"}
# device_fault1-6 = E words, device_fault7-12 = P words (Thermomax app shows
# EFault1-6 / PFault1-6). device_fault7 bit 0 shows as "P01" on the display.
# Other bits are numbered sequentially, 16 codes per word (device_fault8
# bit 0 = P17); only P01 is confirmed.
FAULT_WORDS = {**{n: ("E", n) for n in range(1, 7)}, **{n: ("P", n - 6) for n in range(7, 13)}}
CONFIRMED_CODES = {"P01"}


def num(values: dict[str, Any], key: str) -> float | int | None:
    v = values.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def temp(values: dict[str, Any], key: str) -> float | None:
    v = num(values, key)
    return None if v is None else round(to_signed16(v) / 10, 1)


def _power_reading(values: dict[str, Any], key: str) -> int | None:
    v = num(values, key)
    if v is None or v >= POWER_INVALID_FROM:
        return None
    return int(v)


def electrical_power(values: dict[str, Any]) -> int | None:
    return _power_reading(values, "instantpower")


def heat_output(values: dict[str, Any]) -> int | None:
    """Heat output in W as computed by the pump (flow x ΔT, uncalibrated)."""
    return _power_reading(values, "energy2")


def compressor_hz(values: dict[str, Any]) -> int | None:
    v = num(values, "param1")
    return None if v is None else int(v)


def compressor_running(values: dict[str, Any]) -> bool | None:
    hz = compressor_hz(values)
    return None if hz is None else hz > 0


def delta_t(values: dict[str, Any]) -> float | None:
    t_in, t_out = temp(values, "temp_in"), temp(values, "temp_out")
    if t_in is None or t_out is None:
        return None
    return round(t_out - t_in, 1)


# W; below this (compressor starting or stopping) the heat and power readings
# don't line up and the ratio is meaningless.
COP_MIN_POWER = 300


def cop_from(heat: float | None, power: int | None, running: bool | None) -> float | None:
    """COP, or None when it isn't meaningful (off, starting, no heat reading)."""
    if not running or not heat or power is None or power < COP_MIN_POWER:
        return None
    return round(heat / power, 2)


def cop(values: dict[str, Any]) -> float | None:
    """Instant COP from the heat pump's own (uncalibrated) heat output."""
    return cop_from(heat_output(values), electrical_power(values), compressor_running(values))


def counter_kwh(values: dict[str, Any], key: str) -> float | None:
    v = num(values, key)
    return None if v is None else round(v / 100, 2)


def quiet_mode(values: dict[str, Any]) -> str | None:
    v = num(values, "functionadd")
    if v is None:
        return None
    # Unknown values give None (shown as unknown): an ENUM sensor that reports
    # a state outside its options makes Home Assistant raise on every update.
    return QUIET_MODES.get(int(v))


def zone_on(values: dict[str, Any], zone: int) -> bool | None:
    """onoff1 is a zone bitmask: bit 0 = zone 1, bit 1 = zone 2."""
    v = num(values, "onoff1")
    if v is None:
        return None
    return bool(int(v) & (1 << (zone - 1)))


def flag(values: dict[str, Any], key: str) -> bool | None:
    v = num(values, key)
    return None if v is None else v != 0


def fault_codes(values: dict[str, Any]) -> list[str]:
    codes = []
    for word, (letter, index) in FAULT_WORDS.items():
        v = num(values, f"device_fault{word}")
        if not v:
            continue
        v = int(v)
        for bit in range(16):
            if v & (1 << bit):
                codes.append(f"{letter}{(index - 1) * 16 + bit + 1:02d}")
    return codes
