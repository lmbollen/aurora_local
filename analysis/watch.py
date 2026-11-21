"""Show setting changes and compressor state from the tail of the raw archive.

Usage: python watch.py <latest.jsonl> [since HH:MM:SS]
"""

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
SETTINGS = ("onoff", "onoff1", "hot", "holiday", "functionadd", "mode", "modeset",
            "temp_air2", "temp_water", "temp_task")
since = sys.argv[2] if len(sys.argv) > 2 else "00:00:00"
state: dict = {}
last = None
for line in open(sys.argv[1], encoding="utf-8"):
    rec = json.loads(line)
    if not rec.get("ok"):
        continue
    msg = json.loads(rec["raw"])
    p = msg.get("params", {})
    t = datetime.fromisoformat(rec["ts"]).astimezone(TZ)
    hm = t.strftime("%H:%M:%S")
    for k in SETTINGS:
        if k in p and state.get(k) != p[k]:
            if hm >= since and k in state:
                print(f"{t:%H:%M:%S.%f}"[:-3], f"{k}: {state[k]} -> {p[k]}  (id {msg.get('id')})")
            state[k] = p[k]
    for k, v in p.items():
        if k not in SETTINGS:
            state[k] = v
    if "instantpower" in p and hm >= since:
        s16 = lambda v: v - 65536 if isinstance(v, int) and v >= 32768 else v
        print(f"{hm}  comp={state.get('param1')}Hz power={p['instantpower']}W heat={p.get('energy2')}W "
              f"flow={p.get('instantrate')} in={s16(state.get('temp_in', 0))/10} out={s16(state.get('temp_out', 0))/10} "
              f"target={state.get('temp_curve2', 0)/10} P01={state.get('device_fault7')}")
    last = t
print("current:", {k: state.get(k) for k in SETTINGS}, "| last record", last.strftime("%H:%M:%S"))
