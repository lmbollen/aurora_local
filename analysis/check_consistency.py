"""Check that every field's numbers fit its inferred meaning (docs §5).

Replays an archive and tests physical and cross-field rules on every burst:
refrigerant pressures vs temperatures (R290), discharge/suction/liquid line
vs condensing/evaporating temperature, drives at rest vs running, defrost
and oil-return signatures, the two flow readings, and the duplicates. Each
rule reports how often it holds; it fails if that is below its threshold.

Usage: python check_consistency.py <custom_components/aurora_local> <archive dir>
"""

import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(sys.argv[1])
F = lib.fields
S16 = lib.state.to_signed16

# R290 saturation: °C vs bar absolute
TAB = [(-40, 1.11), (-30, 1.68), (-20, 2.44), (-10, 3.45), (0, 4.74), (10, 6.36), (20, 8.36),
       (30, 10.79), (40, 13.69), (50, 17.13), (60, 21.17), (70, 25.90)]


def tsat(kpa_gauge):
    lp = math.log(kpa_gauge / 100 + 1.013)
    pts = [(t, math.log(p)) for t, p in TAB]
    for (t1, p1), (t2, p2) in zip(pts, pts[1:]):
        if lp <= p2 or t2 == pts[-1][0]:
            return t1 + (lp - p1) / (p2 - p1) * (t2 - t1)
    return pts[-1][0]


def t10(v, key):
    x = F.num(v, key)
    return None if x is None else S16(x) / 10


results = defaultdict(lambda: [0, 0, []])  # rule -> [checked, held, examples]
pairs = defaultdict(list)   # for correlation rules
flows = defaultdict(list)   # controller flow (m³/h) -> raw flow readings


def rule(name, ok, example=None):
    r = results[name]
    r[0] += 1
    if ok:
        r[1] += 1
    elif len(r[2]) < 3 and example is not None:
        r[2].append(example)


state = {"hz": 0, "on_since": None, "off_since": None}


def on_burst(v, ts):
    hz = F.compressor_hz(v) or 0
    if hz > 0 and state["hz"] == 0:
        state["on_since"], state["off_since"] = ts, None
    if hz == 0 and state["hz"] > 0:
        state["off_since"], state["on_since"] = ts, None
    if state["off_since"] is None and state["on_since"] is None:
        state["off_since" if hz == 0 else "on_since"] = ts
    state["hz"] = hz
    running = hz > 0
    run_min = (ts - state["on_since"]) / 60 if running and state["on_since"] else 0
    off_min = (ts - state["off_since"]) / 60 if not running and state["off_since"] else 0
    defrost = F.defrosting(v)
    oil = F.flag(v, F.OIL_RETURN)
    steady = running and run_min >= 10 and not defrost and not oil

    hp, lp = F.num(v, F.HIGH_PRESSURE), F.num(v, F.LOW_PRESSURE)
    if hp is None or lp is None or not 0 < lp < 6000 or not 0 < hp < 6000:
        return
    tc, te = tsat(hp), tsat(lp)
    t_out, t_in, t_amb = F.temp(v, "temp_out"), F.temp(v, "temp_in"), F.temp(v, "temp_envir2")
    te33 = t10(v, F.EVAPORATING_TEMP)
    dis, suc, coil, liq = (t10(v, k) for k in (F.DISCHARGE_TEMP, F.SUCTION_TEMP, F.COIL_TEMP, F.LIQUID_LINE_TEMP))
    cur, dcv, volt = F.tenths(v, F.COMPRESSOR_CURRENT), F.num(v, F.DC_BUS_VOLTAGE), F.num(v, "voltage1")
    fan, eev, pump = F.num(v, F.OUTDOOR_FAN), F.num(v, F.EXPANSION_VALVE), F.num(v, F.CIRCULATION_PUMP)
    power, flow, flow2 = F.electrical_power(v), F.num(v, "instantrate"), F.tenths(v, F.WATER_FLOW_2)
    ex = f"{ts:.0f}"

    # --- pressures and saturation temperatures
    if te33 is not None:
        rule("evaporating temp = Tsat(low pressure) ±0.5 K", abs(te33 - te) <= 0.5, (ex, te33, round(te, 2)))
    if running:
        rule("running: high pressure > low pressure", hp > lp, (ex, hp, lp))
    if not running and off_min >= 30:
        rule("at rest ≥30 min: pressures equal ±50 kPa", abs(hp - lp) <= 50, (ex, hp, lp))
        if t_amb is not None:
            rule("at rest ≥30 min: Tsat within outdoor −6..+3 K", -6 <= tc - t_amb <= 3, (ex, round(tc, 1), t_amb))
    if steady and t_out is not None:
        rule("steady: condensing ≥ leaving water − 1.5 K", tc >= t_out - 1.5, (ex, round(tc, 1), t_out))
    if steady and t_amb is not None:
        rule("steady: evaporating < outdoor + 0.5 K", te < t_amb + 0.5, (ex, round(te, 1), t_amb))
    # --- refrigerant temperatures
    if steady and suc is not None:
        rule("steady: suction ≥ evaporating − 0.5 K", suc >= te - 0.5, (ex, suc, round(te, 1)))
    if steady and dis is not None:
        rule("steady: discharge ≥ condensing − 1 K", dis >= tc - 1, (ex, dis, round(tc, 1)))
        rule("steady: discharge ≥ leaving water − 1 K", dis >= t_out - 1 if t_out is not None else True, (ex, dis, t_out))
    if steady and liq is not None:
        rule("steady: liquid line ≤ condensing + 1 K", liq <= tc + 1, (ex, liq, round(tc, 1)))
        if t_in is not None:
            rule("steady: liquid line ≥ return water − 1 K", liq >= t_in - 1, (ex, liq, t_in))
    if steady and coil is not None and t_amb is not None:
        rule("steady: coil between evaporating − 1 and outdoor + 1 K", te - 1 <= coil <= t_amb + 1, (ex, coil, round(te, 1), t_amb))
    # --- electrical
    if running and cur is not None and power is not None and steady:
        rule("steady: compressor current > 0", cur > 0, (ex, power, cur))
        pairs["power_current"].append((power, cur))
    if not running and off_min >= 5 and cur is not None:
        rule("at rest: compressor current 0", cur == 0, (ex, cur))
    if not running and off_min >= 5 and dcv is not None and volt is not None:
        rule("at rest: DC bus = √2 × mains ± 15 V", abs(dcv - volt * math.sqrt(2)) <= 15, (ex, dcv, volt))
    if running and dcv is not None and volt is not None and steady:
        rule("steady: DC bus ≥ √2 × mains − 15 V", dcv >= volt * math.sqrt(2) - 15, (ex, dcv, volt))
    # --- drives
    if not running and off_min >= 5 and fan is not None:
        rule("at rest: outdoor fan 0", fan == 0, (ex, fan))
    if steady and fan is not None:
        rule("steady: outdoor fan 300–900 rpm", 300 <= fan <= 900, (ex, fan))
    if not running and off_min >= 5 and eev is not None:
        rule("at rest: expansion valve 480 (reset)", eev == 480, (ex, eev))
    if steady and eev is not None:
        rule("steady: expansion valve 40–480", 40 <= eev <= 480, (ex, eev))
    if pump is not None and F.num(v, "param9") is not None:
        rule("pump % is 0 exactly when param9 (pump on) is 0", (pump == 0) == (F.num(v, "param9") == 0), (ex, pump, F.num(v, "param9")))
    # --- events
    if defrost:
        if t_out is not None and t_in is not None:
            rule("defrost: water ΔT < 0", t_out - t_in < 0, (ex, t_out, t_in))
        if fan is not None:
            rule("defrost: outdoor fan 0", fan == 0, (ex, fan))
    if oil and running and eev is not None:
        rule("oil return: expansion valve 480 or start-up value", eev >= 60, (ex, eev))
    # --- flow readings
    if steady and flow is not None and flow2 is not None:
        flows[round(flow2, 1)].append(flow)  # informational: the two readings compared
    if steady and flow2 is not None and pump is not None:
        rule("steady: controller flow > 0 while the pump runs", flow2 > 0 or pump == 0, (ex, flow2, pump))
    # --- duplicates
    for dup, key, tol in (("param3", "temp_envir2", 10), ("param4", "temp_out", 20), ("param30", "temp_in", 10),
                          ("param17", "voltage1", 4), ("param57", "temp_envir1", 10)):
        a, b = F.num(v, dup), F.num(v, key)
        if a is not None and b is not None:
            rule(f"duplicate {dup} = {key} (±{tol} raw)", abs(S16(a) - S16(b)) <= tol, (ex, a, b))


lib.archive.RawLogger(Path(sys.argv[2]), 365).replay(None, on_burst)


def corr(xy):
    n = len(xy)
    mx, my = sum(x for x, _ in xy) / n, sum(y for _, y in xy) / n
    sxy = sum((x - mx) * (y - my) for x, y in xy)
    sx = math.sqrt(sum((x - mx) ** 2 for x, _ in xy))
    sy = math.sqrt(sum((y - my) ** 2 for _, y in xy))
    return sxy / (sx * sy)


r = corr(pairs["power_current"])
results["steady: compressor current tracks power (r ≥ 0.95)"] = [1, int(r >= 0.95), [] if r >= 0.95 else [round(r, 3)]]
print(f"power vs compressor current: r = {r:.3f} (n = {len(pairs['power_current'])})")
print("controller flow (m³/h) vs raw flow reading, steady running (informational):")
for f2 in sorted(flows):
    raw = sorted(flows[f2])
    if len(raw) >= 30:
        print(f"  {f2:4.1f} m³/h: n {len(raw):5d}, raw p10/50/90 {raw[len(raw) // 10]}/{raw[len(raw) // 2]}/{raw[9 * len(raw) // 10]}")
print()

THRESHOLD = {  # minimum share that must hold; physical rules allow sensor lag
    "default": 0.97,
    # A 91-second burst can straddle the start or end of a defrost (3-5 min),
    # so one burst per defrost may show the fan still running or ΔT ≥ 0.
    "defrost: outdoor fan 0": 0.9,
    "defrost: water ΔT < 0": 0.9,
}
failed = []
print(f"{'rule':66s} {'checked':>8s} {'holds':>7s}")
for name, (n, held, examples) in sorted(results.items()):
    share = held / n if n else 1
    need = THRESHOLD.get(name, THRESHOLD["default"])
    mark = "OK " if share >= need else "LOW"
    print(f"{mark} {name:62s} {n:8d} {share:7.1%}" + (f"   e.g. {examples}" if share < 1 else ""))
    if share < need:
        failed.append(name)
assert len(results) >= 25, f"only {len(results)} rules had data"
assert not failed, f"rules below threshold: {failed}"
print("\nall consistency rules hold")
