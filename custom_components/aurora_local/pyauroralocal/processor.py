"""One place that turns a burst of readings into corrected values.

Used for live data, for learning the calibration from the archive at first
start, and for rebuilding history, so all three give the same numbers.
"""

from __future__ import annotations

from typing import Any

from .calibration import CAL_MANUAL, Calibration, EnergyIntegrator
from .monitoring import LowFlowTracker


class BurstProcessor:
    """Calibration + calibrated heat energy + low-flow tracking."""

    def __init__(self, calibration: Calibration | None = None, *, learn: bool = True) -> None:
        self.calibration = calibration or Calibration()
        self.learn = learn  # False: use the calibration as is (history rebuild)
        self.energy = EnergyIntegrator()  # calibrated heat, kWh
        self.low_flow = LowFlowTracker()
        self.corrected: dict[str, float | None] = {}

    @classmethod
    def fixed(cls, inlet_offset: float, outlet_offset: float) -> BurstProcessor:
        """A processor with fixed (manual) offsets that doesn't learn."""
        cal = Calibration()
        cal.mode = CAL_MANUAL
        cal.manual_inlet, cal.manual_outlet = inlet_offset, outlet_offset
        return cls(cal, learn=False)

    def process(self, values: dict[str, Any], ts: float,
                monotonic: float | None = None) -> dict[str, float | None]:
        """Feed one burst (merged values at epoch `ts`); returns corrected values.

        Pass `monotonic` (a monotonic clock reading) to also update the
        low-flow tracker; replays leave it out.
        """
        if self.learn:
            self.calibration.observe(values, ts)
        self.corrected = self.calibration.corrected(values, ts)
        self.energy.add(self.corrected["heat"], ts)
        if monotonic is not None:
            self.low_flow.update(values, now=monotonic, dt=self.corrected["delta_t"])
        return self.corrected

    def recompute(self, values: dict[str, Any], ts: float) -> dict[str, float | None]:
        """Recalculate corrected values after a calibration change (no learning)."""
        self.corrected = self.calibration.corrected(values, ts)
        return self.corrected
