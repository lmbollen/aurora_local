"""Check RawLogger.replay-based bootstrap (what the coordinator runs on first start).

Usage: python test_bootstrap.py <custom_components/aurora_local> <archive_dir>
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(sys.argv[1])

processor = lib.processor.BurstProcessor()  # what the coordinator uses
cal, energy = processor.calibration, processor.energy

t = time.time()
values = lib.archive.RawLogger(Path(sys.argv[2]), 30).replay(None, processor.process)
last_ts = max(ts for ts, _ in cal.samples)
print(f"bootstrap took {time.time() - t:.1f}s; fields {len(values)}; samples {len(cal.samples)}")
print(f"auto offset {cal.auto_offset(last_ts)} K; calibrated heat {energy.kwh:.1f} kWh")
size = len(json.dumps(cal.to_dict()))
print(f"stored calibration size: {size / 1024:.0f} KB")
assert 0.8 <= cal.auto_offset(last_ts) <= 0.9 and 600 < energy.kwh < 700
print("OK")
