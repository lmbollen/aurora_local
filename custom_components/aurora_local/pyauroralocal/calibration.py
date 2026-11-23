"""Inlet/outlet temperature calibration and energy integration."""

from __future__ import annotations

from typing import Any

from .fields import (
    compressor_hz,
    compressor_running,
    cop_from,
    delta_t,
    electrical_power,
    heat_output,
    num,
    temp,
)

CAL_AUTO = "auto"
CAL_MANUAL = "manual"
CAL_SETTLE_SECONDS = 20 * 60     # compressor off this long before sampling
CAL_WINDOW_SECONDS = 7 * 86400   # average over the last 7 days of samples
CAL_MIN_SAMPLES = 30             # don't apply an auto offset before this
CAL_MAX_SAMPLES = 7000           # one per ~91 s of idle time: enough for 7 days
CAL_MAX_GAP_SECONDS = 300        # longer data gap: restart the settle timer
CAL_MAX_ABS_OFFSET = 5.0         # ignore samples that can't be a sensor offset
CAL_MAX_IDLE_POWER = 100         # W; above this the pump isn't idle (standby only)
CAL_CACHE_SECONDS = 60           # reuse the computed offset within a burst
MIN_DELTA_FOR_SCALING = 0.3      # K; below this, heat scaling is unreliable


class Calibration:
    """Inlet/outlet temperature calibration.

    The heat pump computes heat output from flow × (outlet − inlet), so an
    offset between the two sensors directly skews heat output and COP. With
    the compressor off long enough and no water flowing, both sensors sit in
    the same still water and should agree; their difference then is the
    offset. On this unit that's +0.8–0.9 K (outlet reads high), about 30 %
    of a typical running ΔT.

    auto   : offset = interquartile mean of (outlet − inlet) over settled-off
             samples of the last 7 days; applied to the outlet reading
             (inlet as reference).
    manual : user-entered offsets added to each reading.
    """

    def __init__(self) -> None:
        self.mode = CAL_AUTO
        self.manual_inlet = 0.0
        self.manual_outlet = 0.0
        self.samples: list[tuple[float, float]] = []  # (epoch seconds, outlet − inlet)
        self._off_since: float | None = None
        self._last_seen: float | None = None
        self._cache: tuple[tuple[int, float | None], float, float | None] | None = None

    # -- learning ------------------------------------------------------------

    def observe(self, values: dict[str, Any], now: float) -> bool:
        """Feed one measurement burst; returns True if a sample was taken."""
        # After a gap in the data (restart, outage) we don't know whether the
        # compressor ran in between, so the 20-minute settle time starts over.
        if self._last_seen is not None and now - self._last_seen > CAL_MAX_GAP_SECONDS:
            self._off_since = None
        self._last_seen = now
        hz = compressor_hz(values)
        if hz is None:
            return False
        if hz > 0:
            self._off_since = None
            return False
        if self._off_since is None:
            self._off_since = now
            return False
        power = electrical_power(values)
        flow = num(values, "instantrate")
        dt = delta_t(values)
        if (
            now - self._off_since < CAL_SETTLE_SECONDS
            or dt is None
            or abs(dt) > CAL_MAX_ABS_OFFSET
            or (flow is not None and flow > 0)
            or (power is not None and power >= CAL_MAX_IDLE_POWER)
        ):
            return False
        self.samples.append((now, dt))
        if len(self.samples) > CAL_MAX_SAMPLES:
            del self.samples[: len(self.samples) - CAL_MAX_SAMPLES]
        return True

    def recent(self, now: float) -> list[float]:
        return [dt for ts, dt in self.samples if now - ts <= CAL_WINDOW_SECONDS]

    def auto_offset(self, now: float) -> float | None:
        key = (len(self.samples), self.samples[-1][0] if self.samples else None)
        if self._cache and self._cache[0] == key and abs(now - self._cache[1]) < CAL_CACHE_SECONDS:
            return self._cache[2]
        xs = sorted(self.recent(now))
        result = None
        if len(xs) >= CAL_MIN_SAMPLES:
            # Interquartile mean: robust like a median, but resolves finer than
            # the sensors' 0.1 K steps (a plain median only gives 0.8 or 0.9).
            q = len(xs) // 4
            core = xs[q: len(xs) - q] or xs
            result = round(sum(core) / len(core), 2)
        self._cache = (key, now, result)
        return result

    # -- applying ------------------------------------------------------------

    def offsets(self, now: float) -> tuple[float, float]:
        """(add to inlet, add to outlet) in K."""
        if self.mode == CAL_MANUAL:
            return self.manual_inlet, self.manual_outlet
        auto = self.auto_offset(now)
        return 0.0, (-auto if auto is not None else 0.0)

    def corrected(self, values: dict[str, Any], now: float) -> dict[str, float | None]:
        t_in, t_out = temp(values, "temp_in"), temp(values, "temp_out")
        add_in, add_out = self.offsets(now)
        u_in = None if t_in is None else t_in + add_in
        u_out = None if t_out is None else t_out + add_out
        c_in = None if u_in is None else round(u_in, 2)
        c_out = None if u_out is None else round(u_out, 2)
        raw_dt = delta_t(values)
        # ΔT from unrounded values: rounding first would add up to 0.1 K error
        c_dt = None if u_in is None or u_out is None else round(u_out - u_in, 2)
        heat_raw = heat_output(values)
        heat = None
        if heat_raw is not None:
            if heat_raw == 0 or not compressor_running(values):
                heat = heat_raw
            elif raw_dt is not None and c_dt is not None and raw_dt > MIN_DELTA_FOR_SCALING:
                heat = max(0, round(heat_raw * c_dt / raw_dt))
        cop_c = cop_from(heat, electrical_power(values), compressor_hz(values))
        return {"temp_in": c_in, "temp_out": c_out, "delta_t": c_dt, "heat": heat, "cop": cop_c}

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "manual_inlet": self.manual_inlet,
                "manual_outlet": self.manual_outlet, "samples": self.samples[-CAL_MAX_SAMPLES:]}

    def load(self, data: dict[str, Any] | None) -> None:
        if not isinstance(data, dict):
            return
        if data.get("mode") in (CAL_AUTO, CAL_MANUAL):
            self.mode = data["mode"]
        for key in ("manual_inlet", "manual_outlet"):
            try:
                setattr(self, key, float(data.get(key, 0.0)))
            except (TypeError, ValueError):
                pass
        samples = data.get("samples")
        if isinstance(samples, list):
            self.samples = [
                (float(item[0]), float(item[1])) for item in samples
                if isinstance(item, (list, tuple)) and len(item) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in item)
            ][-CAL_MAX_SAMPLES:]
        self._cache = None


class EnergyIntegrator:
    """Integrates a power reading (W) into kWh; gaps over `max_gap` are skipped."""

    def __init__(self, max_gap: float = 300.0) -> None:
        self.kwh = 0.0
        self.max_gap = max_gap
        self._last: tuple[float, float] | None = None  # (ts, watts)

    def add(self, watts: float | None, now: float) -> None:
        if watts is None:
            self._last = None
            return
        if self._last is not None:
            t0, w0 = self._last
            dt = now - t0
            if 0 < dt <= self.max_gap:
                self.kwh += (w0 + watts) / 2 * dt / 3_600_000  # trapezoid
        self._last = (now, watts)
