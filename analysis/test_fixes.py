"""Offline tests for the v0.7.0 fix batch (HA-free modules only).

Usage: python test_fixes.py <custom_components/aurora_local>
"""

import asyncio
import gzip
import json
import logging
import sys
import tempfile
import types
from pathlib import Path

logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(sys.argv[1])

ok = lambda name: print(f"OK  {name}")  # noqa: E731
GOOD = '{"method":"thing.property.post","id":"%d","params":{"deviceKey":"AABBCCDDEEFF","temp_in":%d},"version":"1.0","crc":1}'

# --- parser ----------------------------------------------------------------
p = lib.framing.StreamParser()
corrupt = '{"method":"thing.property.post","id":"1","params":{"deviceKey":"AABBCCDDEEFF","temp_in":1'  # lost "}}"
out = p.feed((corrupt + GOOD % (2, 222)).encode())
msgs = [m for _, m in out if m]
assert [m["id"] for m in msgs] == ["2"], out
assert any(m is None and "temp_in\":1" in raw for raw, m in out)
ok("parser resyncs after a message with a missing closing brace")

p = lib.framing.StreamParser()
stray = '{"method":"x","id":"1","params":{"a":"unterminated}}'  # odd quote count
out = p.feed((stray + GOOD % (3, 1)).encode())
assert [m["id"] for _, m in out if m] == ["3"], out
ok("parser resyncs after a stray quote")

p = lib.framing.StreamParser()
data = ('{"method":"thing.property.post","id":"1","params":{"s":"café"}}').encode()
cut = data.index("é".encode()) + 1  # split inside the 2-byte é
out = p.feed(data[:cut]) + p.feed(data[cut:])
assert out[0][1]["params"]["s"] == "café", out
ok("UTF-8 character split across reads is decoded correctly")

p = lib.framing.StreamParser()
deep = '{"method":"x","params":' + "[" * 100000 + "]" * 100000 + "}"
out = p.feed(deep.encode())
assert all(m is None for _, m in out)
ok("deeply nested JSON is dropped instead of raising")

# --- client lifecycle / backoff ---------------------------------------------
c = lib.client.AdlarClient("x", 1, lambda *a: None)
assert c.backoff_delay(1025) == 3600 and c.backoff_delay(10**6) == 3600
ok("backoff no longer overflows after ~1025 failures (capped at 1 h)")


async def lifecycle():
    c = lib.client.AdlarClient("127.0.0.1", 9, lambda *a: None, backoff_base=0.05, connect_timeout=0.2)
    c.start()
    await asyncio.sleep(0.1)
    await c.stop()
    c.reconnect_now()
    c._task_done(types.SimpleNamespace(cancelled=lambda: False, exception=lambda: RuntimeError("x")))
    await asyncio.sleep(0.2)
    assert c._task is None or c._task.done(), "client revived after stop()"
    assert c.status == lib.client.STATUS_STOPPED


asyncio.run(lifecycle())
ok("stopped client is never revived by reconnect_now or a pending restart")

# --- validation and limits ---------------------------------------------------
assert lib.state.device_key_of({"deviceKey": "AABBCCDDEEFF-01"}) == "AABBCCDDEEFF"
for bad in ("..", "../etc", "a/b", "", "x" * 40, None, 12):
    assert lib.state.device_key_of({"deviceKey": bad}) is None, bad
ok("device keys like '..' or paths are rejected")

st = lib.state.AdlarState()
params = {f"k{i}": i for i in range(lib.state.MAX_FIELDS + 50)}
params.update({"bad key!": 1, "x" * 100: 2, "long": "y" * 1000})
st.apply({"method": "thing.property.post", "params": params})
assert len(st.values) == lib.state.MAX_FIELDS and "bad key!" not in st.values
st2 = lib.state.AdlarState()
st2.apply({"method": "thing.property.post", "params": {"long": "y" * 1000}})
assert len(st2.values["long"]) == lib.state.MAX_VALUE_LENGTH
ok("field count, key format and value length are capped")

# --- values -------------------------------------------------------------------
assert lib.fields.quiet_mode({"functionadd": 7}) is None and lib.fields.quiet_mode({"functionadd": 2}) == "level_2"
ok("unknown quiet-mode value gives None (valid for the ENUM sensor)")

run = {"param1": 40, "temp_in": 300, "temp_out": 330, "instantrate": 500}
assert lib.fields.cop({**run, "energy2": 5203, "instantpower": 244}) is None
assert lib.fields.cop({**run, "energy2": 0, "instantpower": 900}) is None
assert lib.fields.cop({**run, "energy2": 3000, "instantpower": 1000}) == 3.0
cal = lib.calibration.Calibration()
assert cal.corrected({**run, "energy2": 0, "instantpower": 900}, 0)["cop"] is None
ok("COP is unknown at low power (<300 W) or zero heat")

t = lib.monitoring.LowFlowTracker(hold=300, clear_hold=300)
low = {"param1": 40, "instantrate": 200}
good = {"param1": 40, "instantrate": 600}
for s in range(0, 301, 30):
    t.update(low, now=s)
assert t.active(now=300)
t.update(good, now=330)
assert t.active(now=330), "one good reading must not clear the warning"
t.update(low, now=360)
t.update(good, now=390)
assert t.active(now=600)
for s in range(420, 721, 30):
    t.update(good, now=s)
assert not t.active(now=720)
ok("low-flow warning has hysteresis (no 22-second flaps)")

cal = lib.calibration.Calibration()
off = {"param1": 0, "temp_in": 300, "temp_out": 309, "instantpower": 8, "instantrate": 0}
taken = [cal.observe(off, s) for s in range(0, 1300, 90)]
assert not any(taken[:13]) and taken[-1], taken  # samples only after 20 min of continuous data
cal2 = lib.calibration.Calibration()
cal2.observe(off, 0)
cal2.observe(off, 60)
assert not cal2.observe(off, 1300), "a 20-minute gap must restart the settle timer"
ok("calibration settle timer restarts after a data gap")

# --- archive ------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    line = json.dumps({"ts": "2026-11-20T10:00:00+00:00", "raw": GOOD % (1, 250), "ok": True})
    (d / "2026-11-20.jsonl").write_text(line + "\n")
    with gzip.open(d / "2026-11-20.jsonl.gz", "wt") as fh:
        fh.write(line + "\n")
    count = []
    st = lib.state.AdlarState()
    lib.archive.RawLogger(d, 30).replay(None, lambda v, ts: count.append(ts), state=st)
    assert st.methods.get("thing.property.post") == 1, st.methods
    (d / "2026-11-21.jsonl.gz").write_bytes(gzip.compress((line + "\n").encode())[:20])  # truncated
    values = lib.archive.RawLogger(d, 30).replay(None)
    assert values.get("temp_in") == 250
ok("archive replay reads a day once (no double count) and survives a truncated .gz")

print("\nall fix tests passed")
