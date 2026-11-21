"""Turn the raw archive into one row per data burst.

Usage: python build_timeseries.py <archive_dir> <out_dir>
Writes snapshots.csv (one row per burst, all fields as received),
e2_changes.csv (installer parameter changes) and stats.txt (gaps etc.).
"""

import csv
import gzip
import json
import sys
from datetime import datetime
from pathlib import Path

archive, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)


def lines():
    for f in sorted(archive.iterdir()):
        opener = gzip.open if f.suffix == ".gz" else open
        with opener(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


state: dict = {}
e2: dict[int, object] = {}
e2_changes = []
snapshots = []
keys: list[str] = []
prev_ts = None
gaps = []
n = bad = 0
methods: dict[str, int] = {}

for rec in lines():
    n += 1
    ts = datetime.fromisoformat(rec["ts"])
    if prev_ts and (ts - prev_ts).total_seconds() > 150:
        gaps.append((prev_ts, ts))
    prev_ts = ts
    if not rec["ok"]:
        bad += 1
        continue
    msg = json.loads(rec["raw"])
    m = msg.get("method")
    methods[m] = methods.get(m, 0) + 1
    if m != "thing.property.post":
        continue
    for k, v in msg.get("params", {}).items():
        if k in ("deviceKey", "productKey", "fixtime"):
            continue
        if k.startswith("E2_") and isinstance(v, str):
            for part in v.split(";"):
                if ":" in part:
                    a, b = part.split(":", 1)
                    a = int(a)
                    if a in e2 and e2[a] != b:
                        e2_changes.append((ts.isoformat(), a, e2[a], b))
                    e2[a] = b
            continue
        if k not in state:
            keys.append(k)
        state[k] = v
    # "instantpower" closes the measurement part of each burst
    if "instantpower" in msg.get("params", {}):
        snapshots.append({"ts": ts.isoformat(), **state})

with open(out / "snapshots.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, ["ts", *keys])
    w.writeheader()
    w.writerows(snapshots)
with open(out / "e2_changes.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["ts", "param", "old", "new"])
    w.writerows(e2_changes)
with open(out / "e2_final.json", "w") as fh:
    json.dump(e2, fh)
with open(out / "stats.txt", "w") as fh:
    print(f"records: {n}, non-JSON: {bad}, methods: {methods}", file=fh)
    print(f"snapshots: {len(snapshots)}, first {snapshots[0]['ts']}, last {snapshots[-1]['ts']}", file=fh)
    print(f"fields: {len(keys)}", file=fh)
    print(f"gaps > 150 s: {len(gaps)}", file=fh)
    for a, b in gaps:
        print(f"  {a.isoformat()} -> {b.isoformat()} ({(b - a).total_seconds():.0f} s)", file=fh)
    print(f"E2 changes: {len(e2_changes)}", file=fh)
print((out / "stats.txt").read_text())
