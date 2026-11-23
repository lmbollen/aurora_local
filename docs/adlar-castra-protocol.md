# Adlar Castra Aurora III: what we know about the local protocol

Developer material. Findings from probing the heat pump's Wi-Fi module, from
raw data recorded with this integration (the first ~36 hours when it was
still called `adlar_castra`, later 19 days), from comparing the stream with
the vendor app, and from the Aurora III Modbus register map. Sections 1–4
and 6–8 are the original research log. Section 5 (`paramN`) is current: it
was redone on 19 days of data and overrides older statements elsewhere.
Section 9 was written before it, and sections 10–11 hold the app and
settings findings.

Confidence labels used below:

- **Confirmed**: checked against independent evidence (another counter, the
  unit's display, or physics).
- **Likely**: strong correlation or obvious from naming, not independently
  checked.
- **Unknown**: seen, meaning not established.

## 1. Hardware and network

| Item | Value |
|---|---|
| Wi-Fi module | Hi-Flying HF-LPT270. After a power cycle the module once came back with a **different MAC address** (equal to the device key) and therefore a new DHCP address. The MAC can change, so match on the device key, not the IP or MAC. |
| Open ports | TCP **8899** only; no web UI on 80; no reply to HF discovery on UDP 48899 |
| Device ID | `deviceKey`: 12 hex digits, e.g. `AABBCCDDEEFF` (unique per unit); `productKey`: a short alphanumeric key (per product type) |
| Sub-devices | `<deviceKey>-01` … `-07` (only ever seen in `thing.sub.logout`) |
| Authentication | **None.** Anyone on the LAN can read the stream. |

Port 8899 is the module's transparent serial-to-TCP bridge: what the heat
pump's controller writes to its UART comes out as TCP, and **anything written
to the socket goes to the controller**. The integration never writes. We have
not seen any command format.

## 2. Framing and message types

The stream is back-to-back JSON objects with no separator or newline:

```json
{"method":"thing.property.post","id":"3976","params":{"deviceKey":"AABBCCDDEEFF","productKey":"PRODUCTKEY","param1":40},"version":"1.0","crc":5103}
```

- `id` is a decimal string that increments per message and survives
  reconnects (it is the controller's counter, not per-connection).
- `crc` is present on every message: the **byte sum of the `params`
  object text** (confirmed, see §11).
- `version` is always `"1.0"`.
- Values are ints, except installer-table strings (`"1:2;2:5;..."`) and one
  decimal in E2 (`213:1.0`, `249:0.95`).
- In 51,412 messages, **zero** were malformed or non-JSON.

| `method` | Share | Content |
|---|---|---|
| `thing.property.post` | 82% | Measurements, faults, settings, installer table |
| `thing.sub.logout` | 18% | `{deviceKey: "<id>-0N", productKey, bandUser: 2}` for N = 1..7, start of each burst. No useful data (`bandUser` possibly the number of linked app users). |

`thing.property.post` with only `{"fixtime": 1}` arrives roughly every 60 s
between bursts. Looks like a time-sync request to the (absent) cloud.

## 3. Timing and connection behaviour (confirmed)

- **One full burst about every 91 s**: 7 × logout, then ~38 property posts
  ~0.3 s apart, in this fixed order:
  1. `param1`–`param62` (8 messages)
  2. `device_fault1`–`device_fault12` (2)
  3. `temp_*`, `solarenergy`, `smart_grid`, `dmode1`, `group1-4` (2)
  4. `power_use*`, `energy_use*`, `runtime*`, `voltage*`, `current*` (2)
  5. `instantpower`, `energy1`, `energy2`, `instantrate` (1)
  6. `E2_1_10` … `E2_291_300` (15)
- A **settings block** (`tempmin*`/`tempmax*`, `temp_set*`, `onoff`, `mode`,
  … see §6) first appeared at 2026-11-05 07:42, right after a 228 s gap at
  07:34. Since then it is part of every burst. Possibly triggered by the
  module/controller restarting, or by the vendor app being opened.
- The module **closes listen-only TCP clients after ~300 s** (HF default
  "TCP timeout" for clients that never send). A clean close every 5 min is
  normal; reconnecting within 1 s loses nothing.
- After the heat pump is **power-cycled**, the stream pauses ~3 min.
- Volume: ~1,500 messages/hour, ~10 MB/day as JSONL, ~0.45 MB/day gzipped.

## 4. Measurements: field map

Temperatures are **tenths of °C**, signed 16-bit (`65532` = −0.4). All
"lifetime" counters were checked by comparing their change over 34 h with
the integral of the instantaneous values.

| Field | Meaning | Unit / scale | Confidence | Evidence |
|---|---|---|---|---|
| `temp_in` | Water return (inlet) | 0.1 °C | Confirmed (app, §10) | Δ vs `temp_out` 4.5 K when running |
| `temp_out` | Water flow (outlet) | 0.1 °C | Confirmed (app, §10) | Follows `temp_curve2` target |
| `temp_envir2` | **Outdoor air** | 0.1 °C | Confirmed (app, §10) | Daily cycle 0.5 → 9.8 °C, min at dawn |
| `temp_box` | **DHW tank sensor** (app: TempWaterTank) | 0.1 °C | Confirmed (app, §10) | Tracks `temp_envir2` +3 K, DHW off: sensor likely unused |
| `temp_envir1` | **Room temperature** (app: Room Temp) | 0.1 °C | Confirmed (app, §10) | Flat 13.6–15.3 °C |
| `temp_curve2` (= `temp_curve12`, `param48`, `param50`) | **Current target flow temperature** | 0.1 °C | Confirmed (app, §10) | Varies 24–56 °C; water follows it |
| `temp_curve1`, `temp_curve11` | Constant 24.0 °C | 0.1 °C | Unknown | Probably curve minimum |
| `instantpower` | **Electrical input power** | W | Confirmed | Integral 17.3 kWh ≈ `power_use` Δ 17.6 kWh |
| `instantpower` = `32771` | Sentinel / invalid (`0x8003`) | n/a | Likely | Appeared only during restart attempts in a fault |
| `energy2` | **Heat output now** | W | Confirmed | 0 when compressor off; integral 87.0 kWh ≈ `energy_use2` Δ 87.1 kWh. **Not a counter.** |
| `instantrate` | **Water flow** | ~L/h (×1.28?) | Likely | `energy2 ≈ instantrate × ΔT × 1.163 × 1.28`, r = 0.95 (first data). Reads far below the controller's flow (`param38`) during the radiator boost and in defrost, see §5 |
| `energy_use2` | **Lifetime heat produced** | 0.01 kWh | Confirmed | 771 kWh at 2026-11-06 |
| `power_use` | **Lifetime electricity used** | 0.01 kWh | Confirmed | 152 kWh at 2026-11-06 |
| `power_use12` | Lifetime electricity, subset | 0.01 kWh | Likely | Δ 15.0 kWh vs total 17.6, maybe compressor only |
| `runtime2` | **Compressor run time** | minutes | Confirmed | Δ 1,511 min vs 1,514 running minutes measured |
| `*1` / `*3` variants of `energy_use`, `power_use`, `runtime`, `energy` | Always 0 | n/a | Likely | Other modes (cooling / hot water?) never used here |
| `voltage1` | Mains voltage | V | Likely | 216–234 V |
| `current1`, `voltage2/3`, `current2/3` | Always 0 | n/a | Unknown | Single-phase install |
| `dmode1` | Constant 2 | n/a | Unknown | Operating mode? (`mode` = 2 too) |
| `group1`–`group4` | 0/1 flags | n/a | Unknown | `group3` flips with compressor state |
| `solarenergy`, `smart_grid` | Always 0 | n/a | Likely | PV / SG-ready inputs, unused |

Derived values worth exposing:

- **COP** = `energy2 / instantpower` while running. Measured 4.4 at 0–2 °C
  outdoor, 5.7–6.2 above 2 °C, flow 30–32 °C.
- **ΔT** = `temp_out − temp_in`. 4–5 K is normal here; 15 K preceded P01.
- **Seasonal COP** = Δ`energy_use2` / Δ`power_use`. 4.95 over 34 h.

## 5. `param1`–`param62`

Checked on the full archive (17,667 bursts, 2026-11-04 → 2026-11-23) by two
independent analyses. None of the refrigerant fields is shown by the vendor
app, so "Likely" is the best they can be without a manual. Tc and Te below
are the condensing and evaporating temperatures from `param60` and `param58`
(R290 saturation curve).

### Duplicates of named fields

| Param | Duplicate of | Evidence |
|---|---|---|
| `param3` | `temp_envir2` (outdoor) | 86 % identical, 99 % within ±1; gaps only during fast changes |
| `param4` | `temp_out` | 72 % identical, 98 % within ±2 |
| `param17` | `voltage1` | 73 % identical, otherwise ±2/±4 (2 V steps) |
| `param29` | `temp_box` | 85 % identical, otherwise within the sensor step |
| `param30` | `temp_in` | 77 % identical, 99 % within ±1 |
| `param48`, `param50` | `temp_curve2` (target) | 99.65 % identical; mismatches only at setpoint steps |
| `param57` | `temp_envir1` (room) | 88 % identical, otherwise ±1 |
| `param47`, `param49` | `temp_curve1` / `temp_curve11` (240) | 100 %, but all four are constant |

**Not duplicates** (they looked like it in the first 36 hours):

| Param | Meaning | Confidence | Evidence |
|---|---|---|---|
| `param59` | Status flag, unknown | Unknown | 594 bursts with `param59` = 1 and no fault (19–21 Nov, normal running); on in 74 % of bursts with leaving water 45–50 °C, never below 30 °C or below 10 bar |
| `param9` | Circulation pump enabled / unit enabled | Likely | 0 exactly when `param32` = 0 (59 of 59 bursts): all P01 bursts and a 42-minute stop without fault |
| `param10` | **4-way valve in heating position** (0 = defrost) | Likely | 0 in all 33 defrosts while the compressor runs (water ΔT −3.6 K, fan 0, expansion valve 480); otherwise follows `group3` (compressor on) |
| `param53` | Unknown | Unknown | 95 % equal to `group3`; 0 during defrosts and for hours after some oil returns, 1 during pump after-run |

### Refrigerant circuit and drives

| Param | Meaning | Confidence | Evidence |
|---|---|---|---|
| `param1` | **Compressor frequency**, Hz | Confirmed (app) | 0 when off, 15–95 running |
| `param60` | **High (condensing) pressure**, kPa gauge | Likely | Equal to `param58` at standstill; Tc ≈ leaving water while running |
| `param58` | **Low (evaporating) pressure**, kPa gauge | Likely | Best-fitting offset 0.99–1.03 bar (atmospheric, so gauge); at standstill Te is 1 K below outdoor |
| `param33` | **Evaporating temperature**, 0.1 °C, signed | Likely | = Te within 0.1 K (r 1.00): the controller's own conversion of `param58` |
| `param5` | **Discharge temperature**, 0.1 °C | Likely | Max 88.5 °C, up to 41 K above leaving water (rules out a water-side or plate sensor); never below Tc − 0.5 K; equals Tc when suction superheat < 1 K (normal at low speed with the expansion valve at its 60-step minimum), Tc + 12–15 K when superheat > 2 K |
| `param6` | **Suction temperature**, 0.1 °C, signed | Likely | Te + 0.7 K median, + 4.6–8 K at 40–70 Hz, 13–17 °C during defrost |
| `param7` | **Outdoor coil temperature** (defrost sensor), 0.1 °C, signed | Likely | Te + 0.3 K running (r 0.99); stays near 0 °C while ice melts in defrost, then rises to 5–22 °C; outdoor − 0.4 K at standstill |
| `param8` | **Liquid line temperature** (condenser out), 0.1 °C | Likely | Return water + 1.5 K running (r 0.98); falls to outdoor within ~5 min after a stop while the water stays warm, so refrigerant side |
| `param22` | High-side value, whole °C, 0 when off | Unknown | Tc + 2.7 K (r 0.97), gap grows with speed (≈ Tc + 0.11 × Hz) |
| `param24` | Computed value, whole °C steps, 0–22 | Unknown | Not an evaporator sensor (r 0.20 with Te); 0.70 × outdoor + 0.36 × Tc − 10.6 explains 89 % |
| `param16` | Compressor motor current (phase), 0.1 A | Likely | r 0.99 with power; not DC current (power ÷ (current × `param21`) falls from 1.02 to 0.79 with load) |
| `param21` | **DC bus voltage**, V | Likely | 315 V at standstill (= 1.414 × mains − 3 V); 334–353 V running |
| `param2` | **Expansion valve opening**, steps | Likely | 480 at rest and during defrost/oil return; ~200 at start, then 60 (58 % of running time, a minimum) |
| `param25` | **Outdoor fan speed**, rpm | Likely | 0 off and in defrost, 400 ± 3, 450 / 500 at higher speed, ~820 briefly after defrost |
| `param32` | **Circulation pump speed**, % | Likely | 35 standby (no flow), 70 normal, 95–99 when `instantrate` is low |
| `param37` | Follows the pump command | Unknown | r 0.87 with `param32`: 14–15 / 16–17 / 19–22 at 35 / 70 / 99 % |
| `param38` | **Actual water flow**, 0.1 m³/h (Modbus 64) | Likely | r 0.80 with `instantrate`; 0 or 4 standby, 7–8 (700–800 L/h) normal, 9–10 when the pump starts |
| `param39` | Tied to `param38` | Unknown | `param38` + 5 or + 6 (r 0.96) |

### Event flags

| Param | Meaning | Confidence | Evidence |
|---|---|---|---|
| `param18` | **Oil return** | Likely | 55 events mid-run, ~5 bursts each with the expansion valve at 480; every 248 min of compressor run time (median) |
| `param51` | Defrost variant flag | Likely | Set in 12 of 33 defrosts (11 of them the 55 Hz type) |
| `param34` | Idle-only flag | Unknown | 7 windows of 20–30 min, compressor off, pump standby, outdoor −1.7 to 5 °C |
| `param31` | Near-constant 10 | Unknown | 0 for one burst after a compressor stop (40×); 439 once during P01 |

Defrost (33 events, 5–23 Nov, every 0.9–1.9 h around 0 °C outdoor, 3–5
minutes each): `param10` = 0, compressor at 55 or 65 Hz, outdoor fan off,
expansion valve 480, water ΔT −3 to −5 K, `instantrate` ≈ 100 although the
pump runs at 99 %. A negative ΔT alone is not a reliable defrost signal.

### Constant

| Param | Value |
|---|---|
| `param11`–`param15`, `param19`, `param20`, `param23`, `param26`–`param28`, `param35`, `param36`, `param41`, `param52`, `param54`–`param56`, `param62` | 0 |
| `param40` | 15 |
| `param42`, `param44`–`param46`, `param61` | 65036 (−50.0 °C, probably "sensor not fitted") |
| `param43` | 65236 (−30.0 °C) |

### Consistency check

`analysis/check_consistency.py` tests these meanings against physics and
against each other on every burst (17,655 bursts, 4–23 Nov); all 31 rules
hold in 97–100 % of the bursts they apply to. Among them:

- evaporating temperature = R290 saturation of the low pressure (99.9 %);
- at rest ≥ 30 min the pressures are equal (99.9 %) and their saturation
  temperature is near outdoor (99.6 %);
- steady running: condensing ≥ leaving water − 1.5 K (100 %), evaporating
  below outdoor (100 %), suction ≥ evaporating (99.9 %), discharge ≥
  condensing (99.9 %), liquid line between return water and condensing
  (99.9–100 %), coil between evaporating and outdoor (99.9 %);
- at rest: compressor current 0, fan 0, expansion valve 480, DC bus = √2 ×
  mains (99–100 %); running: DC bus above that (100 %); compressor current
  tracks power (r = 0.988);
- pump % is 0 exactly when `param9` is 0 (100 %); defrost: fan 0 and water
  ΔT < 0 (95–97 %, the rest is a burst straddling the start or end);
- the duplicates hold (100 % within the sensor step).

The two flow readings don't follow one scale: at `param38` = 0.5 m³/h the
raw `instantrate` reads ~400, at 0.8 m³/h ~965. The controller's figure
varies less than the raw one, in both directions.

### Cross-check with the Aurora III Modbus register map

The Aurora III also has an RS-485 Modbus interface. A community Homey app
publishes its input-register map with names and units
([org.hhi.adlar3-heatpump-modbus](https://github.com/hhi/org.hhi.adlar3-heatpump-modbus),
`lib/aurora3/protocol/adlar3-modbus-registers.ts`). The Wi-Fi `paramN`
numbers don't follow the register numbers, but the meanings found above
match the map's names one to one, which makes them much stronger:

| Param | Modbus input register | Notes |
|---|---|---|
| `param1` | 79 Compressor actual frequency, Hz | |
| `param2` | 70 Main EEV open degree, pulses | |
| `param3` | 50 Ambient temperature (T4) | |
| `param5` | 52 Compressor discharge temperature (TP) | confirms discharge |
| `param6` | 53 Compressor suction temperature (TH) | confirms suction |
| `param7` | 49 Outdoor unit coil temperature (T3) | |
| `param8` | 51 EXV outlet temperature (T5) | named for cooling; in heating this is the liquid line between plate exchanger and valve |
| `param9` | 80 Main water pump on/off | |
| `param10` | 81 Four-way valve on/off | 0 in defrost |
| `param16` | 77 Compressor phase current, 0.1 A | not DC-link current (see the table above) |
| `param17` | 74 AC input voltage | = `voltage1` |
| `param18` | 38 System status, bit 0: oil return | |
| `param21` | 76 DC bus voltage | |
| `param25` | 72 DC fan speed, rpm | |
| `param30` | 42 Return water temperature (TE1) | |
| `param32` | 62 PWM percentage of DC water pump | |
| `param37` | 63 PWM feedback of DC water pump (0.1 m³/h per the map) | 14–20 → 1.4–2.0? meaning unclear |
| `param38` | 64 Actual water flow, 0.1 m³/h | a second flow reading, 7–8 normally (700–800 L/h) |
| `param58` | 87 Low pressure, kPa | |
| `param60` | 86 High pressure, kPa | |
| `param34` | 38 bit 2 anti-freeze? | candidate: idle, pump on standby, outdoor −1.7 to 5 °C |
| `param51` | 38 bit 1 defrost? | candidate, but set in only 12 of 33 defrosts |
| `param24` | 71 Auxiliary (EVI) EEV open degree? | candidate: 0–220 in steps of 10 |

`param33` (evaporating temperature) and `param22` have no named register;
the map's reserved registers 54/55 may be the saturation temperatures. The
map also has inlet/outlet water pressure (registers 60/61, marked reserved);
no Wi-Fi field behaves like a water pressure, so this unit probably doesn't
report one.

### COP and water flow

At the same lift and compressor speed the measured COP is about 2 points
lower when `instantrate` is low. This is most likely a **measurement
artefact**, not lost efficiency: in cells matched on compressor speed and
both refrigerant pressures (the same refrigerant cycle), electrical power is
equal (+2 %) and the outdoor coil is at least as far below air temperature,
yet the reported heat is 42 % lower. The water ΔT is smaller then (2.2 vs
3.4 K), so the heat can only be equal if the real flow is about 1,250 L/h
while `instantrate` reads about 510. The flow reading (or the heat figure
built on it) under-reads in these conditions, mostly when the target flow
temperature is raised and the circulation pump runs at 95–99 %; it also
reads ≈ 100 during defrost with the pump at 99 %. Based on few matched cells
(3–15 depending on binning). A separate heat meter would settle it. Real
flow stoppages also happen (the P01 faults of 6 Nov had ΔT 15 K).

The second flow reading (`param38`, Modbus "actual water flow") also drops
at those times, but less: median 5 (≈ 500 L/h) when `instantrate` reads
below 450, against 7–8 normally. It probably comes from the same flow
sensor, so it doesn't settle the question either; it does suggest the flow
really falls by about a third, not by the 60 % that `instantrate` shows.

## 6. Settings block (since 2026-11-05 07:42)

Constant unless noted. Temperatures in 0.1 °C. Interpretations are guesses
until compared with the vendor app (see §9).

| Field | Value | Notes |
|---|---|---|
| `onoff` | 0 | Unknown which circuit |
| `onoff1` | 1 → 0 at 11:04:02, → 1 after restart | **Heating on/off.** Changed when the unit was switched off at the display. |
| `mode`, `modeset` | 2, 1 | 2 = heating? |
| `functionadd` | 0 ↔ 2 | **Quiet/Mute mode: 0 Off, 1 On, 2 Level 2** (confirmed, §11). Changed 4×; 2 → 0 at 10:00 just before the P01 event |
| `hot` | 0 | **AHS / forced heating** (confirmed: toggled in the app and seen in the stream, §11) |
| `holiday` | 0 | Holiday mode; turning it on also sets `onoff` = `onoff1` = 0 |
| `temp_water` | 450 | 45 °C, DHW setpoint |
| `temp_air`, `temp_air2`, `temp_set1`, `temp_set2` | 80, 300, 50, 270 | Possibly heating curve points |
| `temp_air3`, `temp_air4`, `temp_set3`, `temp_set4` | 120, 350, 50, 270 | Second curve? |
| `temp_task` | 200 | 20 °C |
| `tempmin1..10`, `tempmax1..10` | e.g. 50/250, 180/750, 200/700 | Allowed ranges for settable values (UI limits) |
| `ksd`, `partition`, `single`, `double`, `single1`, `double1`, `composition`, `dmodel`, `reserve1`, `mode3` | mostly 0/1/3 | Unknown |

## 7. Installer table `E2_1_10` … `E2_291_300`

- 300 parameters, sent as 15 strings of `n:v;` pairs. Values are ints except
  `213` (1.0), `249` (0.95), `250` (1.10), `251` (1.5).
- Only **P204** changes (25–37, 81 changes in 34 h), so it's a live value,
  not a setting. Weak correlation with `temp_in` (r = 0.30).
- Full snapshot: `analysis/out/e2_final.json` (local analysis output, not in the repository).
- Mapping P-numbers to meaning needs the installer manual or the vendor app.

## 8. Faults

| Field | Display code | Confidence | Evidence |
|---|---|---|---|
| `device_fault7` = 1 | **P01** | Confirmed | Display showed P01 while only fault 7 was set |
| `device_fault1..6, 8..12` | Not seen | n/a | Always 0 |

`device_faultN` is probably a bit field (values > 1 possible). Ask the vendor
app or the manual for the full list.

### P01 event, 2026-11-06 (case study)

| Time | What happened |
|---|---|
| ~09:00 | Target flow temperature rises to 56 °C |
| 10:00–10:14 | Compressor 40 → 91 Hz, power 0.9 → 2.7 kW; flow halves (~500 → ~240) |
| 10:15–10:28 | Flow reads 0 at times, compressor still at 91 Hz; outlet 56.3 °C, ΔT 15 K |
| 10:29–10:38 | Compressor stops; three restart attempts; `instantpower` = 32771 between them |
| 10:38 | `device_fault7` latched (P01) |
| 11:04 | `onoff1` 1 → 0 (unit switched off at display) |
| 11:05–11:08 | Data gap: unit power-cycled |
| 11:12–11:14 | P01 again, flow 0, compressor never started; self-cleared at 11:15 |

Interpretation: a **water flow protection**. The data can't distinguish
between a real flow restriction (filter, air, valves, pressure) and a failing
flow sensor or circulation pump.

## 9. Integration changes derived from this research

Written as a to-do list after the first data; everything here is
implemented (0.7–0.8) unless noted.

### Fix wrong definitions (from data above)

- `energy2` → **Heat output**, `device_class: power`, W, `state_class:
  measurement` (first versions used `total_increasing`, which made Home
  Assistant log "not strictly increasing" warnings).
- `power_use` → **Electricity used**, `device_class: energy`, kWh, scale
  0.01, `total_increasing`. Usable in the Energy dashboard.
- `energy_use2` → **Heat produced**, energy, kWh, scale 0.01, `total_increasing`.
- `runtime2` → **Compressor run time**, duration, min, `total_increasing`.
- `instantrate` → **Water flow** (raw unit until checked against a flow meter).
- Use the app's names: `temp_in` → **Return water (RWT)**, `temp_out` →
  **Leaving water (LWT)**, `temp_envir2` → **Outdoor temperature**,
  `temp_envir1` → **Room temperature**, `temp_box` → **DHW tank
  temperature**, `temp_curve2` → **Target flow temperature**.
- `param1` → **Compressor frequency** (Hz, confirmed).
- Add **Zone 1 / Zone 2 on** (bits 0 and 1 of `onoff1`, §10), **Quiet mode**
  (`functionadd`: Off/On/Level 2), **AHS / forced heating** (`hot`),
  **Holiday** (`holiday`).
- Control: not implemented; local control needs the controller's input
  format, which is still unknown (§11).
- Device info: model 7 kW, refrigerant R290, module firmware 0.1.5.
- Treat `instantpower == 32771` (and other `≥ 32768` power values) as
  unavailable rather than 32 kW.

### New features

- Derived sensors: **COP** (instant), **ΔT** (implemented); heat/electric
  energy today (not implemented: use a utility meter helper).
- **Fault code** sensor: map `device_fault7` → "P01" and expose the raw fault
  words for unknown codes. Fire an event on change so automations can alert.
- **Low-flow warning** binary sensor: compressor running and
  `instantrate` < 300 or ΔT > 10 K for 5 min. That would have warned ~15 min
  before P01 tripped.
- Expose `onoff1`, `mode`, `functionadd` as diagnostic sensors with friendly
  names once their values are understood.
- Hide `paramN` duplicates (§5) instead of creating entities for them.

### Robustness

- Raw archive and dynamic entity creation worked as designed: the settings
  block appeared later and got entities automatically.
- Consider a short grace period before marking entities unavailable after a
  power cycle (stream pauses ~3 min; current threshold is 5 min, which is fine).
- Don't attempt writes until the command format is known (the `crc` rule is solved, §11).

## 10. Field meanings confirmed against the vendor app

The heat pump can also be used with the vendor's phone app (Thermomax). With
the app's device screens open next to the local stream, every value shown
matched the local burst from the same minute. That confirmed these
meanings:

| App label | Example value | Local field (and duplicate) | Meaning |
|---|---|---|---|
| RWT / TempIn | 23.6 °C | `temp_in` (`param30`) | **Return water temperature** |
| LWT / TempOut | 27.7 °C | `temp_out` (`param4`) | **Leaving water temperature** |
| Setpoint "CURVE" | 32.0 °C | `temp_curve2` (`param48`, `param50`) | **Target flow temperature** from the weather curve |
| FreqCompressor | 55 Hz | **`param1`** | **Compressor frequency**, Hz |
| TempWaterTank | 10.9 °C | **`temp_box`** (`param29`) | **DHW tank sensor**. On the development unit DHW is off and it tracks outdoor +3 K, so that sensor is probably unused. |
| TempOutdAmbient | 7.4 °C | **`temp_envir2`** (`param3`) | **Outdoor air** |
| Room Temp | 14.2 °C | **`temp_envir1`** (`param57`) | **Room temperature** (where that sensor is) |
| MAC | (device key) | `deviceKey` | The app calls the device key "MAC" |
| Actual Model | 7KW | n/a | Unit size |
| ModuleVersion | 0.1.5 | n/a | Wi-Fi module firmware |
| AirconditionSwitch | "1On 2Off" | `onoff1` = 1, `onoff` = 0 | Heating zone 1 on, zone 2 off (see `onoff1` bitmask below) |
| Mute | Off / On / Level 2 | **`functionadd`** = 0 / 1 / 2 | **Quiet mode** (confirmed by a live test, below) |
| ForcedHeating / AHS | Off | **`hot`** = 0 | **Auxiliary heat source** (backup heater) allowed |
| Holiday | Off | `holiday` = 0 | Holiday mode |
| FixTime | With FixTime | `fixtime` = 1 | Likely |
| HotWaterSwitch | DHW Off | ? (not `hot`) | Unknown |
| Instant efficiency (EER/COP) | 6.71 | `energy2 / instantpower` | Consistent with `energy2` = heat output |
| Mode | Heating | `mode` / `dmode1` = 2 | Likely 2 = heating |
| EFault1–6, PFault1–6 | 0 | `device_fault1–6`, `device_fault7–12` | Fits **`device_fault7` bit 0 = PFault1 → "P01"** |
| FFault1–6, 1EFault1–6, 1PFault1–6 | 0 | not in local stream | Unknown |

The app names this product "O-Household Heat Pump R290(WIFI)", so the
refrigerant is **R290** (propane).

Fault codes: E-words = `device_fault1–6`, P-words = `device_fault7–12`. The
integration numbers the bits sequentially, 16 codes per word: `device_fault7`
bit *n* → P(*n*+1), `device_fault8` bit 0 → P17, and so on. Only P01
(`device_fault7` bit 0) is confirmed on the display.

Other observations:

- `instantpower` ≥ 32768 (e.g. 32771, 32822) also occurs at a normal
  compressor start, not only during a fault. Treat it as invalid.
- `onoff1` is a **zone bitmask**: bit 0 = zone 1, bit 1 = zone 2
  (0 = both off, 1 = zone 1, 2 = zone 2, 3 = both).

## 11. Settings changes as seen locally

Settings were changed with the vendor app while watching the local stream.
In every case the controller **immediately posted the changed property** as
a separate `thing.property.post` (not only in the 91 s burst), about 15 s
after the change was made:

| Change | Local stream | Delay |
|---|---|---|
| Quiet mode → On | `functionadd` 0 → 1 | 17 s |
| Quiet mode → Off | `functionadd` 1 → 0 | 14 s |
| AHS → on / off | `hot` 0 → 1 / 1 → 0 | 15 s |
| Zone 1 off / on | `onoff1` 1 → 0 / 0 → 1 | 15 s / 14 s |

- Quiet level 1 did not visibly cap the compressor (44 → 48 Hz).
- AHS = 1 did not visibly switch on a backup heater; it probably only allows
  the heater when needed.
- **Switching the zone off stops the compressor and circulation pump
  immediately. After switching it on, the compressor restarts only after
  about 8 minutes** (anti-short-cycle delay), so a toggle costs about
  9 minutes of heating.
- Turning on holiday mode in the app also switches both zones off.
- On the development unit, quiet mode switched between 0 and 2 at night and
  in the morning without any app timer being active, so the controller
  probably has its own schedule. A P01 fault once followed right after quiet
  mode ended and the compressor ramped from 40 to 91 Hz.
- After the heat pump has been power-cycled, the settings block is only sent
  when a setting changes. The integration therefore remembers settings
  across restarts.

### Local control: not possible yet

**`crc` solved**: `crc` = **sum of the bytes of the `params` object**,
exactly as serialised in the message (compact JSON, no spaces). Verified on
4,000/4,000 recorded messages (`analysis/crc_search.py`). Example:
`{"deviceKey":"AABBCCDDEEFF","productKey":"PRODUCTKEY","fixtime":1}` → 5232.

A TCP client on port 8899 only receives what the controller sends; commands
reach the controller through the module by another path, so their format
has never been observed. Four plausible frames were sent to port 8899 to set
`functionadd` = 1, each with a correct `crc`, 25 s apart:

| | method | params |
|---|---|---|
| A | `thing.service.property.set` | `{"functionadd":1}` |
| B | `thing.service.property.set` | `{"deviceKey","productKey","functionadd":1}` |
| C | `thing.property.set` | same as B |
| D | `thing.service.property.set` | B with `"functionadd":"1"` |

None produced an echo, reply or error, and the pump state didn't change.
Either the module doesn't forward TCP-client data to the controller in this
firmware (port 8899 is a one-way mirror), or the controller expects another
format. Seeing one real command would settle it, for example by monitoring
the serial line between module and controller with a USB-to-serial adapter.
**Don't experiment with writes on a heat pump you rely on.**

## 12. Open questions

1. Labels for the remaining `paramN` fields (§5: `param22`, `param24`,
   `param31`, `param34`, `param37`, `param39`, `param51`, `param53`,
   `param59`) and the installer table. Sources: the Aurora III Modbus
   register map (§5), and the pump's own display (photograph each info page
   and match against the stream at the same minute).
2. Meaning of `onoff` vs `onoff1` beyond the zone bits, `dmode1`,
   `group1-4`.
3. Exact unit and reliability of `instantrate`: it reads much lower than
   the controller's own flow (`param38`) during the radiator boost and in
   defrost (§5, COP and water flow). A separate flow or heat meter would
   settle it.
4. Meaning of the E2 installer parameters, including live value P204.
5. Full fault-bit → display-code table.
6. The controller's input format (for local control).

## Files

- Integration: `custom_components/aurora_local/` (heat pump library: `custom_components/aurora_local/pyauroralocal/`)
- Raw archive (not in the repository): `config/aurora_local_raw/<device key>/`
- Analysis scripts: `analysis/` (offline; they read a local copy of the raw
  archive)
