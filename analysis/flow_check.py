"""Is instantrate the water flow (L/h)? heat W = flow L/h * 1.163 * dT K."""

import sys
from pathlib import Path

import pandas as pd

out = Path(sys.argv[1])
df = pd.read_csv(out / "snapshots.csv")
df["ts"] = pd.to_datetime(df.ts, format="ISO8601", utc=True).dt.tz_convert("Europe/Amsterdam")
num = df.set_index("ts").apply(pd.to_numeric, errors="coerce")
dT = (num.temp_out - num.temp_in) / 10
run = (num.instantrate > 100) & (dT > 1.5) & (num.energy2 > 0) & (num.energy2 < 20000)
implied = num.energy2[run] / (1.163 * dT[run])
ratio = (implied / num.instantrate[run])
print(f"n={run.sum()}  implied_flow/instantrate: median {ratio.median():.3f}, IQR {ratio.quantile(.25):.3f}..{ratio.quantile(.75):.3f}")
print(f"corr(energy2, instantrate*dT) = {num.energy2[run].corr((num.instantrate*dT)[run]):.3f}")
print(f"instantrate while running: median {num.instantrate[run].median():.0f}, range {num.instantrate[run].min():.0f}..{num.instantrate[run].max():.0f}")
for k in ("param1", "param16", "param38", "param39", "param60"):
    print(f"corr({k}, instantpower running) = {num[k][run].corr(num.instantpower[run]):+.2f}; median {num[k][run].median():g}")
