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
deep = '{"method":"x","params":{"a":' + '{"b":' * 5000 + "1" + "}" * 5000 + "}}"
out = p.feed(deep.encode())  # must not raise, whether or not json parses it
st = lib.state.AdlarState()
for _, m in out:
    if m is not None:
        st.apply(m)  # nesting beyond MAX_NESTING is ignored, no RecursionError
assert st.values == {}, st.values
ok("deeply nested JSON neither raises nor creates fields")

# --- refrigerant fields --------------------------------------------------------
assert lib.fields.pressure_bar({"param60": 1052}, "param60") == 10.52
assert lib.fields.pressure_bar({"param60": 65535}, "param60") is None
assert lib.fields.pressure_bar({"param60": -5}, "param60") is None
assert lib.fields.pressure_bar({}, "param60") is None
ok("refrigerant pressure in bar; out-of-range values are not readings")

assert lib.fields.defrosting({"param1": 55, "param10": 0}) is True
assert lib.fields.defrosting({"param1": 40, "param10": 1}) is False
assert lib.fields.defrosting({"param1": 7, "param10": 0}) is False      # first burst of a start
assert lib.fields.defrosting({"param1": 0, "param10": 0}) is False      # off
assert lib.fields.defrosting({"param1": 40}) is None
assert lib.fields.tenths({"param16": 57}, "param16") == 5.7
assert lib.fields.tenths({"param16": 65535}, "param16") is None
t = lib.monitoring.LowFlowTracker(hold=300, clear_hold=300)
for s in range(0, 601, 30):  # 10 minutes of defrost-like readings
    t.update({"param1": 55, "param10": 0, "param38": 0, "temp_in": 300, "temp_out": 260}, now=s)
assert not t.active(now=600), "defrost must not raise a low-flow warning"
R_DEFROST = {"param1": 55, "param10": 0, "param38": 10}
R_LOW = {"param1": 40, "param10": 1, "param38": 0}
R_GOOD = {"param1": 40, "param10": 1, "param38": 8}
t = lib.monitoring.LowFlowTracker(hold=300, clear_hold=300)
t.update(R_LOW, now=0)                      # low flow pending...
for s in range(30, 601, 30):              # ...then a 10-minute defrost
    t.update(R_DEFROST, now=s)
assert not t.active(now=610), "a pending warning must not complete during defrost"
t = lib.monitoring.LowFlowTracker(hold=300, clear_hold=300)
for s in range(0, 331, 30):
    t.update(R_LOW, now=s)
assert t.active(now=330)
t.update(R_GOOD, now=360)                   # one good reading, then defrost
for s in range(390, 991, 30):
    t.update(R_DEFROST, now=s)
assert t.active(now=1000), "an active warning must not clear during defrost"
ok("defrost detection; a defrost neither raises nor clears a low-flow warning")
assert lib.fields.cop_from(1500, 150, 20) == 10.0     # exactly 150 W and 7.5 W/Hz: shown
assert lib.fields.cop_from(1500, 149, 20) is None      # just below 150 W
assert lib.fields.cop_from(1500, 240, 40) == 6.25      # exactly 6 W/Hz: shown
assert lib.fields.cop_from(1500, 239, 40) is None      # just below 6 W/Hz
ok("COP boundaries: 150 W and 6 W/Hz are inclusive")

# --- defrost heat, energy-based daily COP ---------------------------------------
cal = lib.calibration.Calibration()  # no samples yet: offset 0
defrost = {"param1": 55, "param10": 0, "energy2": 690, "instantpower": 700, "param38": 10,
           "instantrate": 110, "temp_in": 300, "temp_out": 255}
c = cal.corrected(defrost, 0)
assert c["heat"] == -5985, c          # 133 W per (0.1 m³/h · K) × 10 × -4.5 K, not the reported +690 W
assert c["cop"] == -8.55, c           # shown, and negative
lowdt = {"param1": 15, "param10": 1, "energy2": 300, "instantpower": 250, "param38": 8, "temp_in": 300, "temp_out": 302}
assert cal.corrected(lowdt, 0)["heat"] == 213   # ΔT too small to scale: the pump's own formula
assert cal.corrected({**lowdt, "temp_out": 299}, 0)["heat"] == 0  # never negative outside defrost
assert lib.fields.heat_from_flow({"param38": 65535}, 1.0) is None
ok("defrost counts as negative heat (controller flow × ΔT), small ΔT no longer drops heat")

dly = lib.calibration.DailyEnergy()
dly.add("d0", -1000, 9.0, 99.0)                 # tracking starts mid-day: d0 is partial
assert dly.add("d1", 0, 10.0, 100.0) is None and dly.previous is None, "a partial first day is never reported"
dly.add("d1", 90, 10.5, 100.1)
dly.add("d1", 180, 10.3, 100.2)                 # defrost: heat counter goes back
dly.add("d1", 270, 10.4, 99.4)                  # restart: electricity counter steps back
dly.add("d1", 5000, 20.0, 110.0)                # after a 79-minute gap: counts for neither
dly.add("d1", 5090, 20.6, 110.2)
assert round(dly.heat_kwh, 2) == 1.0 and round(dly.elec_kwh, 2) == 0.4, (dly.heat_kwh, dly.elec_kwh)
assert dly.cop() == 2.5
done = dly.add("d2", 5180, 20.7, 110.3)
assert dly.previous == done
assert done[0] == "d1" and round(done[1], 2) == 1.0 and round(done[2], 2) == 0.4, done  # straddling step counts for d2
assert round(dly.heat_kwh, 2) == 0.1 and round(dly.elec_kwh, 2) == 0.1
d2 = lib.calibration.DailyEnergy()
d2.load(dly.to_dict())
assert d2.day == "d2" and d2.previous == dly.previous and round(d2.elec_kwh, 2) == 0.1
assert lib.calibration.energy_cop(1.0, 0.01) is None
ok("daily COP = heat kWh / electricity kWh; defrost counts, counter rollback and gaps don't")

hh = lib.history.HourlyHistory(0, 0, before=3 * 86400, day_of=lambda ts: f"day{int(ts // 86400)}")
heat_on = {"param1": 40, "param10": 1, "instantpower": 1000, "energy2": 3000, "param38": 8, "temp_in": 300, "temp_out": 330}
for i, ts in enumerate(range(43200, 3 * 86400, 90)):  # day0 from noon (partial), day1 complete, day2 complete
    hh.add({**heat_on, "power_use": 10000 + i * 2.5}, ts)  # 1000 W = 2.5 × 0.01 kWh per 90 s
rows = hh.rows()["cop_today"]
starts = [r["start"] for r in rows]
assert min(starts) == 86400 and len(rows) == 24, (len(rows), min(starts))  # day0 partial, day2 not finished
assert all(r["mean"] == r["min"] == r["max"] == 3.0 for r in rows), rows[:2]
ok("history rebuild writes each finished day's energy-based COP into all its hours")

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
assert lib.fields.cop({**run, "param1": 45, "energy2": 5203, "instantpower": 244}) is None  # seen 11-21
assert lib.fields.cop({**run, "energy2": 0, "instantpower": 900}) is None
assert lib.fields.cop({**run, "energy2": 3000, "instantpower": 1000}) == 3.0
assert lib.fields.cop({**run, "param1": 15, "energy2": 1555, "instantpower": 257}) == 6.05
assert lib.fields.cop({**run, "param1": 15, "energy2": 1555, "instantpower": 120}) is None
assert lib.fields.cop({**run, "param1": 0, "energy2": 1555, "instantpower": 257}) is None
cal = lib.calibration.Calibration()
assert cal.corrected({**run, "energy2": 0, "instantpower": 900}, 0)["cop"] is None
assert cal.corrected({**run, "param1": 15, "energy2": 1555, "instantpower": 257}, 0)["cop"] is not None
ok("COP shown at minimum speed (~250 W); unknown in standby, while ramping or at zero heat")

t = lib.monitoring.LowFlowTracker(hold=300, clear_hold=300)
low = {"param1": 40, "param38": 0}
good = {"param1": 40, "param38": 7}
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
t = lib.monitoring.LowFlowTracker(hold=300, clear_hold=300)
for s_ in range(0, 601, 30):  # low COP (instantrate 180) with normal flow: no warning
    t.update({"param1": 60, "param10": 1, "param38": 8, "instantrate": 180, "temp_in": 300, "temp_out": 330}, now=s_)
assert not t.active(now=600)
assert lib.fields.compressor_cop_reported({"instantrate": 412}) == 4.12
assert lib.fields.compressor_cop_reported({"instantrate": 65535}) is None
ok("low flow uses the controller's flow reading; a low COP alone is no flow warning")

cal = lib.calibration.Calibration()
off = {"param1": 0, "temp_in": 300, "temp_out": 309, "instantpower": 8, "param38": 4}  # circulating
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
