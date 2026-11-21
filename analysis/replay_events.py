"""List faults, low-flow episodes (with reason) and data gaps from the archive.

Usage: python replay_events.py <custom_components/aurora_local> <archive_dir>
"""

import gzip
import json
import sys
from collections import Counter
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
tracker = lib.monitoring.LowFlowTracker()
prev_codes, prev_ts = [], None
lf_start, lf_reasons = None, Counter()
episodes, faults, gaps = [], [], []
bad = 0
for rec in records(Path(sys.argv[2])):
    t = datetime.fromisoformat(rec["ts"])
    if prev_ts and (t - prev_ts).total_seconds() > 300:
        gaps.append((prev_ts, t))
    prev_ts = t
    if not rec["ok"]:
        bad += 1
        continue
    msg = json.loads(rec["raw"])
    state.apply(msg)
    v = state.values
    codes = lib.fields.fault_codes(v)
    if codes != prev_codes:
        faults.append((t, prev_codes, codes))
        prev_codes = codes
    if "instantpower" in msg.get("params", {}):
        ts = t.timestamp()
        tracker.update(v, now=ts)
        if tracker.reason:
            lf_reasons[tracker.reason.split()[0]] += 1
        active = tracker.active(now=ts)
        if active and lf_start is None:
            lf_start = (t, tracker.reason, lib.fields.num(v, "instantrate"), lib.fields.delta_t(v))
        elif not active and lf_start is not None:
            episodes.append((*lf_start, t))
            lf_start = None

loc = lambda t: t.astimezone(TZ).strftime("%m-%d %H:%M")
print(f"period {loc(datetime.fromisoformat(next(records(Path(sys.argv[2])))['ts']))} -> {loc(prev_ts)}, non-JSON records: {bad}")
print(f"\n== fault code changes ({len(faults)})")
for t, a, b in faults:
    print(f"  {loc(t)}  {a} -> {b}")
print(f"\n== data gaps > 5 min ({len(gaps)})")
for a, b in gaps:
    print(f"  {loc(a)} -> {loc(b)}  ({(b - a).total_seconds() / 60:.0f} min)")
print(f"\n== low-flow warning episodes ({len(episodes)})")
for start, reason, flow, dt, end in episodes:
    print(f"  {loc(start)} -> {loc(end)}  ({(end - start).total_seconds() / 60:.0f} min)  first reason: {reason}")
print("\nbursts flagged by rule:", dict(lf_reasons))
