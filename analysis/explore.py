"""First pass over snapshots.csv: field inventory, temps, power, counters."""

import sys
from pathlib import Path

import pandas as pd

out = Path(sys.argv[1])
df = pd.read_csv(out / "snapshots.csv")
df["ts"] = pd.to_datetime(df.ts, format="ISO8601", utc=True).dt.tz_convert("Europe/Amsterdam")
df = df.set_index("ts")
num = df.apply(pd.to_numeric, errors="coerce")
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 30)

print("== field inventory (varying fields) ==")
inv = pd.DataFrame({
    "first_seen": num.apply(lambda s: s.first_valid_index()),
    "n": num.count(),
    "min": num.min(), "max": num.max(), "nunique": num.nunique(),
})
print(inv[inv["nunique"] > 1].sort_values("first_seen").to_string())
print("\nconstant fields:", ", ".join(f"{k}={num[k].dropna().iloc[0]:g}" for k in inv[inv["nunique"] == 1].index))
late = inv[inv.first_seen > df.index[0] + pd.Timedelta(minutes=10)]
print("\nfields first seen later:", late[["first_seen", "n"]].to_string())

s16 = lambda s: s.where(s < 32768, s - 65536)
t = pd.DataFrame({k: s16(num[k]) / 10 for k in ["temp_in", "temp_out", "temp_envir1", "temp_envir2", "temp_box"]})
t["delta_t"] = t.temp_out - t.temp_in
print("\n== temperatures, hourly mean ==")
hourly = t.resample("1h").mean().round(1)
hourly["power_W"] = num.instantpower.resample("1h").mean().round(0)
hourly["rate"] = num.instantrate.resample("1h").mean().round(0)
hourly["dmode1"] = num.dmode1.resample("1h").agg(lambda s: s.mode().iloc[0] if len(s) else None)
print(hourly.to_string())

print("\n== power ==")
p = num.instantpower
print(p.describe().round(0).to_string())
on = p > 150
starts = (on & ~on.shift(fill_value=False)).sum()
print(f"share of snapshots running (>150 W): {on.mean():.0%}; starts: {starts}")
dt_h = df.index.to_series().diff().dt.total_seconds().fillna(0) / 3600
dt_h = dt_h.clip(upper=0.1)
kwh_integrated = (p * dt_h).sum() / 1000
span_h = (df.index[-1] - df.index[0]).total_seconds() / 3600
print(f"span {span_h:.1f} h; integrated electrical energy from instantpower: {kwh_integrated:.2f} kWh")

print("\n== counters: first, last, delta, decreases ==")
for k in [c for c in num.columns if any(x in c for x in ("energy", "power_use", "runtime"))]:
    s = num[k].dropna()
    if s.empty:
        continue
    d = s.diff()
    print(f"{k:14s} first={s.iloc[0]:>9g} last={s.iloc[-1]:>9g} delta={s.iloc[-1]-s.iloc[0]:>8g} "
          f"steps_up={(d>0).sum():4d} steps_down={(d<0).sum():3d} max_drop={d.min():g}")

print("\n== energy2 / energy_use2 drops (with local time) ==")
for k in ("energy2", "energy_use2", "power_use", "power_use12", "runtime2"):
    s = num[k].dropna()
    drops = s[s.diff() < 0]
    for ts, v in drops.items():
        print(f"{k} drop at {ts:%a %H:%M:%S}: {s.shift()[ts]:g} -> {v:g}")

print("\n== E2 changes ==")
e2c = pd.read_csv(out / "e2_changes.csv")
e2c["ts"] = pd.to_datetime(e2c.ts, format="ISO8601", utc=True).dt.tz_convert("Europe/Amsterdam")
print(e2c.groupby("param").agg(n=("new", "size"), values=("new", lambda v: sorted(set(v))[:8])).to_string())
