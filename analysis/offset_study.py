"""How do inlet/outlet readings differ while the compressor is off?

Usage: python offset_study.py <custom_components/aurora_local> <archive_dir>
Groups off-periods by how long the compressor has been off and whether water
is circulating, and prints the outlet-inlet difference per group and per day.
"""

import gzip
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(sys.argv[1])
TZ = ZoneInfo("Europe/Amsterdam")


def records(d: Path):
    for f in sorted(d.iterdir(), key=lambda p: p.name.split(".")[0]):
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                yield json.loads(line)


state = lib.state.AdlarState()
off_since = None
groups = defaultdict(list)
per_day = defaultdict(list)
running = []
for rec in records(Path(sys.argv[2])):
    if not rec["ok"]:
        continue
    msg = json.loads(rec["raw"])
    state.apply(msg)
    if "instantpower" not in msg.get("params", {}):
        continue
    v = state.values
    t = datetime.fromisoformat(rec["ts"])
    hz, dt, flow = lib.fields.compressor_hz(v), lib.fields.delta_t(v), lib.fields.num(v, "instantrate")
    power = lib.fields.electrical_power(v)
    if hz is None or dt is None:
        continue
    if hz > 0:
        off_since = None
        running.append(dt)
        continue
    off_since = off_since or t
    mins = (t - off_since).total_seconds() / 60
    band = "0-5" if mins < 5 else "5-15" if mins < 15 else "15-30" if mins < 30 else "30-60" if mins < 60 else "60+"
    circ = "flow>0" if flow else "flow=0"
    groups[(circ, band)].append(dt)
    if mins >= 20 and power is not None and power < 100:
        per_day[(t.astimezone(TZ).strftime("%m-%d"), circ)].append(dt)


def summary(xs):
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    return f"n={len(xs):5d} median={st.median(xs):+.2f} IQR=[{q(.25):+.2f},{q(.75):+.2f}] mean={st.mean(xs):+.2f}"


print("compressor running, ΔT:", summary(running))
print("\ncompressor off, outlet - inlet by circulation and minutes since stop:")
for key in sorted(groups):
    print(f"  {key[0]:7s} {key[1]:>5s} min  {summary(groups[key])}")
print("\nsettled (off ≥20 min, power <100 W), per day:")
for key in sorted(per_day):
    print(f"  {key[0]} {key[1]:7s} {summary(per_day[key])}")
