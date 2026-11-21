"""Hourly history rebuilt from the raw archive.

Used to backfill long-term statistics so history shows the corrected values,
with the pump's own readings kept separately as "reported". The current
calibration offsets are applied uniformly to the whole archive: the sensor
offset is a fixed property of the sensors, and this way re-running after
changing the calibration rewrites history consistently.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from . import fields as F
from .archive import RawLogger
from .processor import BurstProcessor

# key -> function(values, corrected) -> value
MEASURES: dict[str, Callable[[dict[str, Any], dict[str, Any]], float | None]] = {
    "temp_in": lambda v, c: c["temp_in"],
    "temp_out": lambda v, c: c["temp_out"],
    "delta_t": lambda v, c: c["delta_t"],
    "heat_output": lambda v, c: c["heat"],
    "cop": lambda v, c: c["cop"],
    "temp_in_reported": lambda v, c: F.temp(v, "temp_in"),
    "temp_out_reported": lambda v, c: F.temp(v, "temp_out"),
    "delta_t_reported": lambda v, c: F.delta_t(v),
    "heat_output_reported": lambda v, c: F.heat_output(v),
    "cop_reported": lambda v, c: F.cop(v),
}
ENERGY_KEY = "heat_produced_calibrated"


class HourlyHistory:
    """Collects bursts into hourly mean/min/max and an energy counter."""

    def __init__(self, inlet_offset: float, outlet_offset: float, before: float) -> None:
        self.processor = BurstProcessor.fixed(inlet_offset, outlet_offset)
        self.before = before  # only hours starting before this epoch (complete hours)
        self._acc: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.energy_at_hour_end: dict[int, float] = {}
        self.bursts = 0

    def add(self, values: dict[str, Any], ts: float) -> None:
        corrected = self.processor.process(values, ts)
        hour = int(ts // 3600 * 3600)
        if hour + 3600 > self.before:
            return
        self.bursts += 1
        self.energy_at_hour_end[hour] = self.processor.energy.kwh
        for key, fn in MEASURES.items():
            try:
                value = fn(values, corrected)
            except Exception:
                value = None
            if value is not None:
                self._acc[key][hour].append(float(value))

    def rows(self) -> dict[str, list[dict[str, float]]]:
        """{key: [{start, mean, min, max}]} and the energy counter rows."""
        out: dict[str, list[dict[str, float]]] = {}
        for key, hours in self._acc.items():
            out[key] = [
                {"start": h, "mean": round(sum(xs) / len(xs), 3), "min": min(xs), "max": max(xs)}
                for h, xs in sorted(hours.items())
            ]
        # Energy: state = sum = calibrated kWh since the start of the archive
        out[ENERGY_KEY] = [
            {"start": h, "state": round(kwh, 4), "sum": round(kwh, 4)}
            for h, kwh in sorted(self.energy_at_hour_end.items())
        ]
        return out


def build(raw_logger: RawLogger, inlet_offset: float, outlet_offset: float,
          before: float) -> tuple[dict, float]:
    """Blocking: replay the whole archive. Returns (rows, total calibrated kWh)."""
    hist = HourlyHistory(inlet_offset, outlet_offset, before)
    raw_logger.replay(None, hist.add)
    return hist.rows(), hist.processor.energy.kwh
