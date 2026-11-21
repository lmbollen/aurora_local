"""Offline test of history.build on the archive.

Usage: python test_history.py <custom_components/aurora_local> <archive_dir>
"""

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(sys.argv[1])

before = int(time.time() // 3600 * 3600)
t = time.time()
rows, total = lib.history.build(lib.archive.RawLogger(Path(sys.argv[2]), 30), 0.0, -0.86, before)
print(f"build took {time.time() - t:.1f}s, calibrated heat total {total:.1f} kWh")
for key in sorted(rows):
    r = rows[key]
    print(f"  {key:26s} {len(r):4d} hours  first {datetime.fromtimestamp(r[0]['start'], UTC):%m-%d %H:%M}Z"
          f"  last {datetime.fromtimestamp(r[-1]['start'], UTC):%m-%d %H:%M}Z")

# all hours complete and on the hour
for key, r in rows.items():
    assert all(x["start"] % 3600 == 0 and x["start"] + 3600 <= before for x in r), key
# energy monotonic
e = [x["sum"] for x in rows[lib.history.ENERGY_KEY]]
assert all(b >= a for a, b in zip(e, e[1:])) and abs(e[-1] - total) < 5, (e[-1], total)
# corrected vs reported relationship in a running hour
by = {k: {x["start"]: x for x in v} for k, v in rows.items()}
hour = max(h for h in by["heat_output"] if by["heat_output"][h]["mean"] > 2000)
ho, hr = by["heat_output"][hour]["mean"], by["heat_output_reported"][hour]["mean"]
to, tr = by["temp_out"][hour]["mean"], by["temp_out_reported"][hour]["mean"]
ti, tir = by["temp_in"][hour]["mean"], by["temp_in_reported"][hour]["mean"]
print(f"sample hour {datetime.fromtimestamp(hour, UTC):%m-%d %H:%M}Z: heat {ho:.0f} vs reported {hr:.0f} W;"
      f" outlet {to:.2f} vs {tr:.2f} °C; inlet {ti:.2f} vs {tir:.2f} °C;"
      f" COP {by['cop'][hour]['mean']:.2f} vs {by['cop_reported'][hour]['mean']:.2f}")
assert abs((tr - to) - 0.86) < 0.02 and abs(ti - tir) < 0.01 and ho < hr
print("OK")
