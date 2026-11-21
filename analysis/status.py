"""Summarize the pump's recent state from the tail of the raw archive.

Usage: python status.py <latest.jsonl>
Prints one line per measurement burst (local time) and the newest values.
"""

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
state: dict = {}
rows = []
for line in open(sys.argv[1], encoding="utf-8"):
    rec = json.loads(line)
    if not rec.get("ok"):
        continue
    msg = json.loads(rec["raw"])
    if msg.get("method") != "thing.property.post":
        continue
    params = msg.get("params", {})
    state.update(params)
    if "instantpower" in params:  # end of the measurement part of a burst
        ts = datetime.fromisoformat(rec["ts"]).astimezone(TZ)
        rows.append((ts, dict(state)))

s16 = lambda v: v - 65536 if isinstance(v, int) and v >= 32768 else v
print("time   power  heat  flow  comp  in    out   target outdoor fault7 faults")
for ts, st in rows[-15:]:
    faults = [k.replace("device_fault", "") for k, v in st.items() if k.startswith("device_fault") and v]
    print(f"{ts:%H:%M}  {st.get('instantpower'):>5} {st.get('energy2'):>5} {st.get('instantrate'):>5} "
          f"{st.get('param1'):>4}  {s16(st.get('temp_in', 0))/10:4.1f}  {s16(st.get('temp_out', 0))/10:4.1f}  "
          f"{st.get('temp_curve2', 0)/10:4.1f}   {s16(st.get('temp_envir2', 0))/10:4.1f}   "
          f"{st.get('device_fault7')}      {','.join(faults) or '-'}")
if rows:
    print(f"\nbursts in window: {len(rows)}, newest {rows[-1][0]:%Y-%m-%d %H:%M:%S %Z}")
    since = [ts for ts, st in rows if st.get("device_fault7")]
    clear = [ts for ts, st in rows if not st.get("device_fault7")]
    if since and clear and clear[-1] > since[-1]:
        print(f"fault7 cleared; last seen {since[-1]:%H:%M}, clear since {next(t for t, s in rows if t > since[-1] and not s.get('device_fault7')):%H:%M}")
    print("settings:", {k: state.get(k) for k in ("onoff", "onoff1", "mode", "functionadd", "temp_water", "hot")})
