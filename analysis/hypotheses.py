"""Second pass: test what the raw counters and unknown params mean."""

import sys
from pathlib import Path

import pandas as pd

out = Path(sys.argv[1])
df = pd.read_csv(out / "snapshots.csv")
df["ts"] = pd.to_datetime(df.ts, format="ISO8601", utc=True).dt.tz_convert("Europe/Amsterdam")
df = df.set_index("ts")
num = df.apply(pd.to_numeric, errors="coerce")
s16 = lambda s: s.where(s < 32768, s - 65536)
pd.set_option("display.width", 200, "display.max_rows", 200)

dt_h = num.index.to_series().diff().dt.total_seconds().fillna(0).clip(upper=300) / 3600
p = num.instantpower

print("== instantpower outliers (>5000) ==")
print(num.loc[p > 5000, ["instantpower", "energy2", "instantrate", "temp_in", "temp_out"]].to_string())
pc = p.where(p < 5000)

print("\n== counter scale tests ==")
el_kwh = (pc.fillna(0) * dt_h).sum() / 1000
th_kwh = (num.energy2.where(num.energy2 < 20000).fillna(0) * dt_h).sum() / 1000
running_min = ((pc > 150) * dt_h).sum() * 60
span = (num.index[-1] - num.index[0]).total_seconds()
for k in ("power_use", "power_use12", "energy_use2", "runtime2"):
    s = num[k].dropna()
    print(f"{k:12s} delta={s.iloc[-1]-s.iloc[0]:6g}  lifetime={s.iloc[-1]:g}")
print(f"integrated instantpower (outliers removed): {el_kwh:.2f} kWh")
print(f"integrated energy2 as W:                    {th_kwh:.2f} kWh")
print(f"compressor running time (power>150 W):      {running_min:.0f} min of {span/60:.0f} min")
print(f"ratio thermal/electric (integrated):         {th_kwh/el_kwh:.2f}")
print(f"ratio energy_use2/power_use deltas:          "
      f"{(num.energy_use2.iloc[-1]-num.energy_use2.iloc[0])/(num.power_use.iloc[-1]-num.power_use.iloc[0]):.2f}")

print("\n== COP while running vs outdoor (envir2) and flow temp ==")
run = (pc > 300) & (num.energy2 > 0) & (num.energy2 < 20000)
cop = (num.energy2 / pc)[run]
tbl = pd.DataFrame({"cop": cop, "outdoor": s16(num.temp_envir2)[run] / 10, "flow": num.temp_out[run] / 10,
                    "power": pc[run], "rate": num.instantrate[run]})
tbl["outdoor_bin"] = pd.cut(tbl.outdoor, [-5, 2, 4, 6, 8, 12])
print(tbl.groupby("outdoor_bin", observed=True).agg(n=("cop", "size"), cop=("cop", "median"),
      flow=("flow", "median"), power=("power", "median")).round(2).to_string())
print("corr power~instantrate:", round(pc[run].corr(num.instantrate[run]), 3))

print("\n== device_fault7 transitions ==")
f7 = num.device_fault7
for ts in f7.index[f7.diff().fillna(0) != 0]:
    print(f"  {ts:%a %H:%M:%S} -> {int(f7[ts])}")
print(f"  share of time =1: {f7.mean():.0%}")

print("\n== temp_curve2/12 (target?) vs outdoor ==")
tgt = pd.DataFrame({"curve2": num.temp_curve2 / 10, "outdoor": s16(num.temp_envir2) / 10,
                    "flow": num.temp_out / 10}).resample("1h").mean().round(1)
print(tgt.to_string())

print("\n== E2 param 204 vs known fields ==")
e2c = pd.read_csv(out / "e2_changes.csv")
e2c["ts"] = pd.to_datetime(e2c.ts, format="ISO8601", utc=True).dt.tz_convert("Europe/Amsterdam")
p204 = e2c[e2c.param == 204].set_index("ts").new.astype(float)
joined = pd.merge_asof(p204.rename("p204").to_frame(), num[["temp_envir1", "temp_envir2", "temp_box", "temp_in"]]
                       .apply(s16), left_index=True, right_index=True)
print(joined.corr().round(2).loc["p204"].to_string())
print(joined.head(10).to_string())

print("\n== paramN that mirror a named field ==")
named = [c for c in num.columns if not c.startswith("param") and num[c].nunique() > 1]
for c in sorted([c for c in num.columns if c.startswith("param") and num[c].nunique() > 1],
                key=lambda x: int(x[5:])):
    best = max(named, key=lambda n: abs(num[c].corr(num[n])) if num[n].std() > 0 else 0)
    r = num[c].corr(num[best])
    same = (num[c] == num[best]).mean()
    print(f"{c:8s} ~ {best:14s} r={r:+.2f} identical={same:.0%}  range {num[c].min():g}..{num[c].max():g}")

print("\n== settings block (appeared Thu 07:42) ==")
cols = ["temp_water", "temp_air", "temp_air2", "temp_set1", "temp_set2", "temp_air3", "temp_air4",
        "temp_set3", "temp_set4", "temp_task", "mode", "modeset", "onoff", "onoff1", "functionadd", "hot", "holiday"]
print(num[cols].dropna().iloc[-1].to_string())
fa = num.functionadd.dropna()
print("functionadd transitions:", [(f"{t:%a %H:%M}", int(v)) for t, v in fa[fa.diff().fillna(0) != 0].items()])

print("\n== Fri 10:00 high-temp period ==")
w = num.loc["2026-11-06 09:50":"2026-11-06 10:56", ["temp_in", "temp_out", "instantpower", "energy2",
                                                    "instantrate", "temp_curve2", "functionadd", "device_fault7"]]
print(w.iloc[::3].to_string())
