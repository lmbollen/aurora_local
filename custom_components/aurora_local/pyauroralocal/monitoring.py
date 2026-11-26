"""Conditions worth alerting on: low water flow and fault codes."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .fields import WATER_FLOW_2, compressor_running, defrosting, delta_t, fault_codes, tenths

# Low-flow warning thresholds, on the controller's flow reading (param38,
# 0.1 m³/h). It reads 0 below ~0.4 m³/h (no values 1-3 seen while running;
# 4-11 otherwise). Before the P01 of 2026-11-06 it read 0 with ΔT 10-12 K at
# 88 Hz. `instantrate` is not a flow (it is the pump's compressor COP × 100)
# and is no longer used here: on the archive it raised 13 warnings, 11 of
# them with normal flow and only a low COP.
LOW_FLOW_M3H = 0.4
HIGH_DELTA_T = 10.0
LOW_FLOW_HOLD_SECONDS = 300


class LowFlowTracker:
    """Flags sustained low flow / high ΔT while the compressor runs.

    Hysteresis: turns on after the condition has held for `hold` seconds, and
    off only after it has been absent for `clear_hold` seconds. Without the
    second part a single good reading ended the warning, so it flapped on and
    off within seconds.
    """

    def __init__(self, hold: float = LOW_FLOW_HOLD_SECONDS,
                 clear_hold: float = LOW_FLOW_HOLD_SECONDS) -> None:
        self.hold = hold
        self.clear_hold = clear_hold
        self.since: float | None = None        # condition present since
        self.clear_since: float | None = None  # condition absent since
        self.reason: str | None = None
        self._active = False

    def update(self, values: dict[str, Any], now: float | None = None, dt: float | None = None) -> None:
        """`dt` overrides the raw ΔT (pass the calibrated one)."""
        now = time.monotonic() if now is None else now
        if defrosting(values):
            # During defrost the flow reading drops to ~100 with the pump at
            # full speed and ΔT turns negative: neither start nor end a
            # warning on these readings. Pause both timers, so a pending
            # change can't complete during the defrost either (active() is
            # also evaluated between updates); they restart afterwards.
            self.since = None
            self.clear_since = None
            return
        reason = None
        if compressor_running(values):
            flow = tenths(values, WATER_FLOW_2)
            if dt is None:
                dt = delta_t(values)
            if flow is not None and flow < LOW_FLOW_M3H:
                reason = f"water flow {flow} m³/h, below {LOW_FLOW_M3H}"
            elif dt is not None and dt > HIGH_DELTA_T:
                reason = f"ΔT {dt} K, above {HIGH_DELTA_T} K"
        if reason is None:
            self.since = None
            if self.clear_since is None:
                self.clear_since = now
        else:
            self.clear_since = None
            if self.since is None:
                self.since = now
            self.reason = reason
        self._evaluate(now)

    def _evaluate(self, now: float) -> None:
        if not self._active and self.since is not None and now - self.since >= self.hold:
            self._active = True
        elif self._active and self.clear_since is not None and now - self.clear_since >= self.clear_hold:
            self._active = False
            self.reason = None

    def active(self, now: float | None = None) -> bool:
        self._evaluate(time.monotonic() if now is None else now)
        return self._active


@dataclass
class FaultChange:
    """What `FaultTracker.update` reports."""

    codes: list[str]                                   # active now
    previous: list[str]                                # active before
    new: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    still_active: bool = False  # unchanged, but the first check after a restart


class FaultTracker:
    """Follows the fault codes and reports changes.

    `restored` is the list saved before a restart: the first update compares
    against it, so a fault that started (or is still active) while the
    program was not running is reported.
    """

    def __init__(self, restored: list[str] | None = None) -> None:
        self.codes: list[str] | None = None
        self._restored = list(restored) if restored else []

    def update(self, values: dict[str, Any]) -> FaultChange | None:
        if not any(k.startswith("device_fault") for k in values):
            return None
        codes = fault_codes(values)
        first = self.codes is None
        prev = self._restored if first else self.codes
        self.codes = codes
        if codes == prev:
            return FaultChange(codes, prev, still_active=True) if first and codes else None
        return FaultChange(codes, prev,
                           new=[c for c in codes if c not in prev],
                           cleared=[c for c in prev if c not in codes])

    @property
    def last_known(self) -> list[str]:
        """Codes to remember across a restart: the live ones once seen,
        otherwise those restored at start (so a short run can't lose them)."""
        return list(self.codes) if self.codes is not None else list(self._restored)
