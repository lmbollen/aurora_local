"""Conditions worth alerting on: low water flow and fault codes."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .fields import compressor_running, defrosting, delta_t, fault_codes, num

# Low-flow warning thresholds (P01 on 2026-11-06 was preceded by flow
# ~240 and ΔT 15 K while the compressor ran at 91 Hz).
LOW_FLOW_RATE = 300
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
            # warning on these readings.
            return
        reason = None
        if compressor_running(values):
            flow = num(values, "instantrate")
            if dt is None:
                dt = delta_t(values)
            if flow is not None and flow < LOW_FLOW_RATE:
                reason = f"flow reading {flow}, below {LOW_FLOW_RATE}"
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
