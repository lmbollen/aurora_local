"""Replay the archive through Calibration: learned offset over time, corrected
COP and corrected heat energy vs the pump's own counters.

Usage: python replay_calibration.py <custom_components/aurora_local> <archive_dir>
"""

import gzip
import json
import statistics as st
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(sys.argv[1])
TZ = ZoneInfo("Europe/Amsterdam")

# unit checks
c = lib.calibration.Calibration()
v = {"param1": 40, "temp_in": 300, "temp_out": 330, "energy2": 4000, "instantpower": 1000, "instantrate": 500}
assert c.corrected(v, 0)["heat"] == 4000  # no samples yet -> no correction
c.samples = [(0, 0.85)] * 40
r = c.corrected(v, 10)
assert r["temp_out"] == 32.15 and r["delta_t"] == 2.15 and r["heat"] == round(4000 * 2.15 / 3.0), r
c.mode = lib.calibration.CAL_MANUAL; c.manual_inlet = 0.1; c.manual_outlet = -0.5
r = c.corrected(v, 10)
assert r["temp_in"] == 30.1 and r["temp_out"] == 32.5 and r["delta_t"] == 2.4, r
c2 = lib.calibration.Calibration(); c2.load(c.to_dict()); assert c2.mode == "manual" and len(c2.samples) == 40
off = lib.calibration.Calibration()
s = {"param1": 0, "temp_in": 300, "temp_out": 309, "instantpower": 8, "instantrate": 0}
taken = [off.observe(s, t) for t in range(0, 1300, 90)]  # one burst per ~90 s
assert not any(taken[:13]) and taken[-1], taken
assert not off.observe({**s, "param1": 30}, 1350) and not off.observe(s, 1440)  # restarted -> reset
print("unit checks: OK")


def records(d: Path):
    for f in sorted(d.iterdir(), key=lambda p: p.name.split(".")[0]):
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                yield json.loads(line)


state = lib.state.AdlarState()
cal = lib.calibration.Calibration()
e_raw, e_cal = lib.calibration.EnergyIntegrator(), lib.calibration.EnergyIntegrator()
first = last = None
cop_raw, cop_cal = [], []
offset_by_day = {}
for rec in records(Path(sys.argv[2])):
    if not rec["ok"]:
        continue
    msg = json.loads(rec["raw"])
    state.apply(msg)
    if "instantpower" not in msg.get("params", {}):
        continue
    t = datetime.fromisoformat(rec["ts"])
    now = t.timestamp()
    v = state.values
    cal.observe(v, now)
    r = cal.corrected(v, now)
    e_raw.add(lib.fields.heat_output(v), now)
    e_cal.add(r["heat"], now)
    if lib.fields.cop(v) is not None and r["cop"] is not None:
        cop_raw.append(lib.fields.cop(v)); cop_cal.append(r["cop"])
    offset_by_day[t.astimezone(TZ).strftime("%m-%d")] = (cal.auto_offset(now), len(cal.recent(now)))
    first = first or (t, dict(v), now)
    last = (t, dict(v), now)

print("\nauto offset at end of each day (K, samples in 7-day window):")
print("  " + "  ".join(f"{d}:{o if o is not None else '—'}({n})" for d, (o, n) in offset_by_day.items()))
(t0, v0, _), (t1, v1, _) = first, last
d_counter = (v1["energy_use2"] - v0["energy_use2"]) / 100
d_elec = (v1["power_use"] - v0["power_use"]) / 100
print(f"\nperiod {t0.astimezone(TZ):%m-%d %H:%M} -> {t1.astimezone(TZ):%m-%d %H:%M}")
print(f"electricity (counter):           {d_elec:8.1f} kWh")
print(f"heat, pump counter:              {d_counter:8.1f} kWh  -> SCOP {d_counter / d_elec:.2f}")
print(f"heat, integrated reported:       {e_raw.kwh:8.1f} kWh  -> SCOP {e_raw.kwh / d_elec:.2f}")
print(f"heat, integrated calibrated:     {e_cal.kwh:8.1f} kWh  -> SCOP {e_cal.kwh / d_elec:.2f}"
      f"  ({(e_cal.kwh / e_raw.kwh - 1) * 100:+.0f} %)")
print(f"instant COP median: reported {st.median(cop_raw):.2f}, calibrated {st.median(cop_cal):.2f}")
