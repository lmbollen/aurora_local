"""Replay the raw archive through the pyauroralocal library (no Home Assistant).

Usage: python replay_model.py <custom_components/aurora_local> <archive_dir>
"""

import gzip
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

pkg_dir, archive = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(str(pkg_dir))
TZ = ZoneInfo("Europe/Amsterdam")

# --- unit checks ---
assert lib.fields.fault_codes({"device_fault7": 1}) == ["P01"]
assert lib.fields.fault_codes({"device_fault1": 0b100}) == ["E03"]
assert lib.fields.fault_codes({"device_fault8": 1}) == ["P17"]
assert lib.fields.zone_on({"onoff1": 1}, 1) is True and lib.fields.zone_on({"onoff1": 1}, 2) is False
assert lib.fields.zone_on({"onoff1": 3}, 2) is True and lib.fields.zone_on({"onoff1": 0}, 1) is False
assert lib.fields.electrical_power({"instantpower": 32771}) is None
assert lib.fields.quiet_mode({"functionadd": 2}) == "level_2"
assert lib.fields.temp({"temp_envir2": 65532}, "temp_envir2") == -0.4
print("unit checks: OK")


def records():
    for f in sorted(archive.iterdir()):
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                yield json.loads(line)


state = lib.state.AdlarState()
parser_ok = 0
tracker = lib.monitoring.LowFlowTracker()
prev_warn = False
prev_codes: list = []
first = last = None
cops = []
for rec in records():
    if not rec["ok"]:
        continue
    msg = json.loads(rec["raw"])
    state.apply(msg)
    parser_ok += 1
    t = datetime.fromisoformat(rec["ts"])
    ts = t.timestamp()
    v = state.values
    if "instantpower" in msg.get("params", {}):
        tracker.update(v, now=ts)
        warn = tracker.active(now=ts)
        if warn != prev_warn:
            print(f"{t.astimezone(TZ):%a %H:%M:%S}  low-flow warning -> {warn}  ({tracker.reason})")
            prev_warn = warn
        c = lib.fields.cop(v)
        if c is not None:
            cops.append(c)
        first = first or (t, dict(v))
        last = (t, dict(v))
    codes = lib.fields.fault_codes(v)
    if codes != prev_codes:
        print(f"{t.astimezone(TZ):%a %H:%M:%S}  fault codes {prev_codes} -> {codes}")
        prev_codes = codes

print(f"\nmessages applied: {parser_ok}")
(t0, v0), (t1, v1) = first, last
print(f"period {t0.astimezone(TZ):%a %H:%M} -> {t1.astimezone(TZ):%a %H:%M}")
print(f"electricity consumed: {lib.fields.counter_kwh(v0,'power_use')} -> {lib.fields.counter_kwh(v1,'power_use')} kWh")
print(f"heat produced:        {lib.fields.counter_kwh(v0,'energy_use2')} -> {lib.fields.counter_kwh(v1,'energy_use2')} kWh")
print(f"COP while running: median {sorted(cops)[len(cops)//2]}, min {min(cops)}, max {max(cops)}, n={len(cops)}")
print("latest:", {
    "return": lib.fields.temp(v1, "temp_in"), "leaving": lib.fields.temp(v1, "temp_out"), "outdoor": lib.fields.temp(v1, "temp_envir2"),
    "room": lib.fields.temp(v1, "temp_envir1"), "dhw": lib.fields.temp(v1, "temp_box"), "target": lib.fields.temp(v1, "temp_curve2"),
    "power": lib.fields.electrical_power(v1), "heat": lib.fields.heat_output(v1), "cop": lib.fields.cop(v1), "dT": lib.fields.delta_t(v1),
    "compressor_hz": lib.fields.compressor_hz(v1), "quiet": lib.fields.quiet_mode(v1), "zone1": lib.fields.zone_on(v1, 1),
    "zone2": lib.fields.zone_on(v1, 2), "ahs": lib.fields.flag(v1, "hot"), "holiday": lib.fields.flag(v1, "holiday"),
})
