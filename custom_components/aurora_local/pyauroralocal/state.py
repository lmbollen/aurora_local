"""Merged view of everything the heat pump has reported.

Lenient by design (fields may be added, renamed or retyped by firmware
updates) and bounded (the stream is unauthenticated)."""

from __future__ import annotations

import math
import re
import time
from typing import Any

IDENTITY_KEYS = {"deviceKey", "productKey"}
# The device key becomes a config-entry unique id and a folder name, so only
# plain alphanumeric keys are accepted (the real one is 12 hex digits).
DEVICE_KEY_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
FIELD_KEY_RE = re.compile(r"^[A-Za-z0-9_.]{1,64}$")
MAX_FIELDS = 1000
MAX_TABLES = 20
MAX_TABLE_ENTRIES = 1000
MAX_VALUE_LENGTH = 255
MAX_METHODS = 50       # distinct message methods counted (diagnostics)
MAX_NESTING = 4        # nested params deeper than this are ignored
TABLE_RE = re.compile(r"^\s*(-?\d+\s*:[^;]*;\s*)+$")
NUMBER_RE = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")


def device_key_of(params: Any) -> str | None:
    """Device key from a message's params ("<key>" or "<key>-01"), validated."""
    key = params.get("deviceKey") if isinstance(params, dict) else None
    if not isinstance(key, str):
        return None
    key = key.split("-", 1)[0]
    return key if DEVICE_KEY_RE.match(key) else None


def _number(raw: str) -> int | float | str:
    raw = raw.strip()
    if NUMBER_RE.match(raw):
        return float(raw) if "." in raw else int(raw)
    return raw


def parse_table(value: str) -> dict[int, Any]:
    """Parse a parameter table like '1:2;2:5;213:1.0;'."""
    result: dict[int, Any] = {}
    for part in value.split(";"):
        key, sep, raw = part.partition(":")
        if not sep:
            continue
        try:
            result[int(key)] = _number(raw)
        except ValueError:
            continue
    return result


def normalize(value: Any) -> Any:
    """Numeric strings -> numbers; non-finite numbers -> None; rest unchanged."""
    if isinstance(value, str) and NUMBER_RE.match(value):
        return _number(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None  # 1e999 / NaN would break int() and arithmetic later
    return value


def to_signed16(value: Any) -> Any:
    """Values like 65532 are signed 16-bit registers (-4)."""
    if isinstance(value, int) and not isinstance(value, bool) and 32768 <= value <= 65535:
        return value - 65536
    return value


def _flatten(params: dict[str, Any], prefix: str = "", depth: int = 0) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in params.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            if depth < MAX_NESTING:
                flat.update(_flatten(value, f"{name}.", depth + 1))
        else:
            flat[name] = value
    return flat


class AdlarState:
    """Merged view of everything the heat pump has reported.

    `bursts` counts completed measurement bursts (one every ~91 s); derived
    values such as calibration and energy should be computed once per burst.
    """

    def __init__(self) -> None:
        self.device_key: str | None = None
        self.product_key: str | None = None
        self.values: dict[str, Any] = {}
        self.tables: dict[str, dict[int, Any]] = {}
        self.methods: dict[str, int] = {}
        self.last_update: float | None = None  # time.monotonic()
        self.bursts = 0  # completed measurement bursts

    def apply(self, msg: dict[str, Any]) -> tuple[bool, set[str], set[str]]:
        """Merge one message.

        Returns (had_values, new_value_keys, new_table_names).
        """
        method = str(msg.get("method"))[:64]
        if method in self.methods or len(self.methods) < MAX_METHODS:
            self.methods[method] = self.methods.get(method, 0) + 1
        params = msg.get("params")
        if not isinstance(params, dict):
            return False, set(), set()
        # Logout/login-style messages carry only identity and bookkeeping.
        if method.startswith("thing.sub."):
            return False, set(), set()

        new_keys: set[str] = set()
        new_tables: set[str] = set()
        had_values = False
        if "instantpower" in params:
            # instantpower closes the measurement part of each ~91 s burst
            self.bursts += 1
        for key, value in _flatten(params).items():
            if key == "deviceKey":
                if isinstance(value, str) and "-" not in value and DEVICE_KEY_RE.match(value):
                    self.device_key = value
                continue
            if key == "productKey":
                if isinstance(value, str) and DEVICE_KEY_RE.match(value):
                    self.product_key = value
                continue
            # Limits: the stream is unauthenticated, so a misbehaving device
            # must not be able to create unbounded fields/entities/storage.
            if not FIELD_KEY_RE.match(key):
                continue
            if isinstance(value, str) and TABLE_RE.match(value):
                name = key.split("_", 1)[0]
                if name not in self.tables:
                    if len(self.tables) >= MAX_TABLES:
                        continue
                    self.tables[name] = {}
                    new_tables.add(name)
                table = self.tables[name]
                for idx, item in parse_table(value).items():
                    if idx in table or len(table) < MAX_TABLE_ENTRIES:
                        table[idx] = item
                had_values = True
                continue
            if key not in self.values:
                if len(self.values) >= MAX_FIELDS:
                    continue
                new_keys.add(key)
            if isinstance(value, str) and len(value) > MAX_VALUE_LENGTH:
                value = value[:MAX_VALUE_LENGTH]
            self.values[key] = normalize(value)
            had_values = True
        if had_values:
            self.last_update = time.monotonic()
        return had_values, new_keys, new_tables

    def age(self) -> float | None:
        """Seconds since the last update with values, or None if none yet."""
        if self.last_update is None:
            return None
        return time.monotonic() - self.last_update
