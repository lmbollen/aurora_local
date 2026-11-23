# Aurora Local: Adlar Castra heat pump in Home Assistant

A Home Assistant integration that reads an **Adlar Castra Aurora** heat pump
directly from its Wi-Fi module on your own network. It needs no cloud account
and no app: the heat pump streams its data and Home Assistant listens.

You get temperatures, power, heat output, COP (efficiency), energy counters,
the heat pump's settings, fault codes and connection status. An offset
between the two water temperature sensors is measured and corrected
automatically, which makes heat output and COP much more accurate (see
[Temperature calibration](#temperature-calibration)).

> **Read-only.** The integration never sends anything to the heat pump, so it
> can't change settings or switch the heat pump on or off. Use the heat pump's
> own display or app for that.

> Not affiliated with, or endorsed by, Adlar, Castra or the makers of the
> Thermomax app (the vendor's phone app). Use at your own risk.

## Is this for you?

It works with heat pumps whose Wi-Fi module is a **Hi-Flying HF-LPT270**
that streams data on **TCP port 8899** of your network. It was developed on
one Adlar Castra Aurora III (7 kW, R290 refrigerant); other Aurora models
with the same Wi-Fi module will probably work, but haven't been tested.

**Find the module's IP address** in your router's list of connected devices
(the module may show up with a name like "HF-LPT270").

**The easiest test is the setup itself:** install the integration and add it
with that IP address (see [Setup](#setup)). Setup only listens; if the heat
pump answers, it works. If not, you get an error and can remove it again.

<details>
<summary>Advanced: check from a computer first</summary>

From a Linux or macOS computer on the same network (use your module's
address instead of `192.168.1.50`):

```bash
nc 192.168.1.50 8899
```

Within about two minutes you should see text like
`{"method":"thing.property.post",...`. **Don't type anything into this
window**: whatever you send goes to the heat pump's controller. Press
**Ctrl+C** to quit, and close it before setting up the integration (the
module allows only a few connections). On Windows,
`Test-NetConnection 192.168.1.50 -Port 8899` in PowerShell only shows
whether the port is open.
</details>

**The vendor app keeps working.** The Thermomax app talks to the heat pump
through the vendor's cloud; during development it kept working while this
integration was connected.

## Requirements

- Home Assistant **2026.10 or newer**
- The heat pump's Wi-Fi module on the same network as Home Assistant
- Recommended: a **fixed IP address** for the module (a DHCP reservation in
  your router)

## Installation

**With HACS** (recommended)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/lmbollen/aurora_local` with category **Integration**.
2. Search for **Aurora Local** in HACS and download it.
3. Restart Home Assistant.

**Manually**

1. Copy the folder `custom_components/aurora_local` into your Home Assistant
   configuration folder, so you get `config/custom_components/aurora_local/`.
2. Restart Home Assistant.

## Setup

1. **Settings → Devices & services → Add integration → Aurora Local**.
2. Enter the module's IP address as **Host**; leave **Port** at **8899**.
3. Wait: setup can take **up to 2 minutes** while it waits for the heat pump
   to identify itself.

| Setup message | What it means |
|---|---|
| *Could not connect, or the module closed the connection* | Wrong IP address, or another program is connected to the module (it accepts only a few connections). Check the address, close other tools, try again. |
| *Connected, but the heat pump sent no data within 2 minutes* | Something answered on that address but no heat pump data arrived. Check that the heat pump is powered, wait a minute and try again. |

### Coming from `adlar_castra`

Before version 0.8.0 this integration was called `adlar_castra`. To move an
existing install over without losing history:

1. Install Aurora Local (above) and restart. **Don't delete the Adlar Castra
   entry** under Devices & services: the takeover needs it.
2. **Add integration → Aurora Local → Take over the adlar_castra
   installation**, and submit.
3. Remove Adlar Castra in HACS (or delete
   `config/custom_components/adlar_castra/`) and restart.

Your device, entities (same entity IDs, with their history and statistics),
settings, temperature calibration and raw archive move over, and the old
entry is removed. Notifications and the network search stay switched on, as
they were. Automations that use the `adlar_castra_*` events or the
`adlar_castra.rebuild_history` action need the new `aurora_local_` names.

## First steps

- Values appear within about 1½ minutes and then update at that pace.
- Add **Electricity consumed** to the **Energy dashboard** under
  *Individual devices*.
- **Quiet mode, Zone 1/2, AHS and Holiday mode** may show *unknown* at first:
  some heat pumps only report settings when they change. Change one on the
  heat pump's display, or wait; once seen they're remembered.
- **COP** shows *unknown* whenever the compressor is off or starting: that's
  normal (see [What you see](#what-you-see)).
- The automatic temperature correction needs the heat pump to stand still
  for a while first (see [Temperature calibration](#temperature-calibration)).
- Want alerts? Turn on **Show notifications** under **Configure** (the gear
  icon on the integration's page), or use the [events](#alerts).

## What you see

The device page shows **corrected** values by default. For the values affected
by the temperature correction, the heat pump's own uncorrected figure is in a
twin sensor named "(reported)" under *Diagnostic*.

**Sensors**

| Sensor | What it shows |
|---|---|
| Return water temperature, Leaving water temperature | Water flowing back into and out of the heat pump (corrected) |
| Flow/return difference | Leaving − return, in kelvin (K: degrees of difference) (corrected) |
| Target flow temperature | The water temperature the heat pump is aiming for, from its weather-dependent heating curve |
| Outdoor temperature, Room temperature | As measured by the heat pump |
| DHW tank temperature | Domestic hot water tank sensor |
| Electrical power | Power the heat pump uses now, in W |
| Heat output | Heat it delivers now, in W (corrected) |
| COP | Heat output ÷ electrical power (corrected). Only while the compressor runs and delivers heat; *unknown* in standby (below 150 W) and while the compressor speeds up or slows down, when the two readings don't line up |
| Electricity consumed | The heat pump's own lifetime electricity counter, kWh |
| Heat produced since install (corrected) | Heat delivered, kWh (corrected), counted by this integration from its first start. It doesn't count while Home Assistant is off |
| Compressor frequency, Compressor run time | Compressor speed (Hz) and total running time |
| Water flow (raw) | Water flow as reported by the heat pump; the unit isn't known, so it has none. Useful for spotting drops (on the development unit typically 700–1,050 while running) |
| Voltage | Mains voltage |
| Quiet mode | off / on / level_2 |
| Fault code | e.g. `P01`, `none` when there is no fault, *unknown* before the heat pump has reported its fault status |

To compare efficiency over a day or a season, divide the daily change of
**Heat produced since install (corrected)** by that of **Electricity
consumed** (for example with Home Assistant's statistics card).

**On/off states**

| Binary sensor | Meaning |
|---|---|
| Fault | A fault is active (codes in its attributes) |
| Low flow warning | Water flow too low for 5 minutes while the compressor runs, not counting defrosts (see [Alerts](#alerts)) |
| Compressor running | The compressor is on |
| Defrost | The outdoor unit is defrosting (a few minutes, every 1–2 hours around 0 °C outside); the water briefly cools then |
| Oil return | The compressor's periodic oil-return cycle, about every 4 hours of running (Diagnostic) |
| Zone 1, Zone 2 | Heating circuit switched on |
| AHS (forced heating) | Auxiliary heat source (backup heater) allowed |
| Holiday mode | Holiday mode is on |

**Configuration** (on the device page): **Temperature calibration** (auto /
manual) and the **Inlet / Outlet temperature offset** settings. The offsets
are used in manual mode; changing one switches calibration to manual.

**Diagnostic** (on the device page):
- the "(reported)" twins, including **Heat produced, lifetime (reported)**:
  the heat pump's own lifetime heat counter, which counts from installation
  and is uncorrected, so it is much higher than the integration's counter;
- **Temperature calibration offset**, **Connection**, **Receiving data** and
  the **Reconnect now** button;
- **Installer parameters**: the number of installer (E2) parameters the
  heat pump reported; the values are in its attributes (P1, P2, …) for
  reference, but their meanings aren't documented;
- raw mode values and curve temperatures whose meaning isn't confirmed;
- the refrigerant circuit and drives: **Refrigerant high pressure** and
  **Refrigerant low pressure** (bar, gauge; at standstill both settle at the
  same value), **Evaporating temperature**, **DC bus voltage**, **Expansion
  valve opening**, **Outdoor fan speed** and **Circulation pump speed** (%).
  Also **Compressor discharge / suction temperature**, **Outdoor coil
  temperature**, **Liquid line temperature**, **Compressor current** and
  **Water flow (controller)**, the controller's own flow figure in m³/h
  (0.1 m³/h steps), next to *Water flow (raw)*.
  The vendor app doesn't show these. Their meanings were worked out from the
  data and match the names in the Aurora III Modbus register map (see the
  protocol notes), so treat them as very likely rather than confirmed. The
  circulation pump reads 35 % at standstill, which is a standby value, not a
  speed.

*Electricity consumed (subset)* is disabled by default. The heat pump also
sends many fields whose meaning isn't known yet. They're created
automatically as diagnostic sensors but **disabled by default**; enable any
you're curious about on the device page (*+N entities not shown*).

## Alerts

The binary sensors above (**Fault**, **Low flow warning**, **Receiving
data**) and the events below always work. In addition, the integration can
show **notifications** (the bell in the sidebar); they're **off by
default**. Turn them on with **Show notifications** under **Settings →
Devices & services → Aurora Local → Configure**. You then get one when:

- **No data for 5 minutes**: "Heat pump offline". It disappears by itself
  when data comes back.
- **A fault code appears**, for example `P01` (on the development unit
  this was a water-flow protection). The notification lists all active
  codes, is updated when one clears and disappears when all have cleared.
  A fault that started while Home Assistant was off is reported too, and a
  fault that's still active after a restart is shown again.
- **Low flow warning** turns on: the flow reading below 300 (or more than
  10 K between leaving and return water) for 5 minutes while the compressor
  runs. Defrosts don't count: the flow reading drops then by design, and
  the timers pause until the defrost ends. It turns off again after 5
  minutes of normal flow, and its
  notification disappears by itself. On the development unit this came about
  15 minutes before a `P01` fault, so it's an early prompt to check the water
  filter, system pressure and valves.

If the module is found at a new IP address, a **repair** (Settings → Repairs)
suggests giving it a fixed address.

**Events for automations**

| Event | When | Data |
|---|---|---|
| `aurora_local_fault` | The fault codes change | `codes`, `new`, `cleared`, `previous`, `device_key` |
| `aurora_local_low_flow` | Low flow warning turns on or off | `active`, `reason` |
| `aurora_local_connection` | No data for 5 minutes, and again when data is back | `connected` (and `last_error`, `failures` when it goes offline) |
| `aurora_local_host_changed` | The module was found at a new address | `old`, `new` |

Example, a fault notification on your phone: **Settings → Automations →
Create automation → ⋮ → Edit in YAML**, paste this and replace
`notify.mobile_app_your_phone` with your own phone's notify action:

```yaml
alias: Heat pump fault to phone
triggers:
  - trigger: event
    event_type: aurora_local_fault
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.new | length > 0 }}"
actions:
  - action: notify.mobile_app_your_phone
    data:
      message: "Heat pump fault {{ trigger.event.data.new | join(', ') }}"
```

Fault codes other than `P01` are inferred from the data format and haven't
been confirmed on a display yet.

## Connection

The **Connection** sensor shows `connected`, `connecting`,
`waiting_to_retry`, `searching` or `stopped`, with the last error and the time
of the next attempt in its attributes.

- If the module can't be reached, or sends nothing for 2½ minutes, the
  integration keeps retrying: after 5 seconds, then doubling the wait each
  time, up to **once an hour**. Press **Reconnect now** to try immediately.
- The module disconnects listeners every 5 minutes by design; the integration
  reconnects within a second, so you won't notice.
- The module accepts only a few connections at a time. If setup fails, close
  other tools connected to port 8899.

**If the module changes IP address** (without a DHCP reservation):

- Set the new address yourself: **Settings → Devices & services → Aurora
  Local → ⋮ → Reconfigure**. The integration checks it's the same heat pump.
- Or press **Reconnect now** while it's failing: the next failed attempt
  then searches once for the module (see below).
- Or turn on **Search the network when the module's address changes** under
  **Configure** (off by default): after 3 failed attempts, at most every 10
  minutes, the integration checks the other addresses in the module's range
  (e.g. 192.168.1.1–254) on port 8899 and recognises your heat pump by its
  device ID. It only connects and listens, never sends, but other devices
  that use port 8899 (such as solar loggers) or network security tools may
  notice it. The search only covers private IPv4 networks (/24), not a
  hostname.

## Temperature calibration

*Return* is the water coming back to the heat pump (the "inlet"); *leaving*
is the water going out to your heating (the "outlet").

The heat pump calculates heat output from water flow × (leaving − return).
That difference is only a few degrees, so a sensor that reads a fraction of a
degree off makes heat output and COP noticeably wrong. On the development
unit the leaving-water sensor read **0.86 K too high**; with a typical
difference of 3–5 K, the heat pump reported 20–40 % more heat (and COP) than
it delivered. Your unit may differ.

**Automatic (default).** When the compressor has been off for at least 20
minutes, no water is flowing and power is below 100 W, both sensors sit in the
same still water and should agree. The integration then measures their
difference once per data update (about every 1½ minutes), averages the
measurements of the last 7 days (ignoring outliers), and corrects the
leaving-water reading. It needs at least 30 measurements before it applies a
correction: about 45 minutes of such idle time, after the first 20 minutes
of each idle stretch (so at least about an hour in total). **Temperature
calibration offset** (Diagnostic) shows the correction applied to leaving −
return: for example −0.86 K when the leaving sensor reads high.

**Manual.** If your heat pump rarely stands still for that long, set
**Temperature calibration** to `manual`, then adjust **Inlet temperature
offset** and **Outlet temperature offset**: kelvin added to each reading,
−5 to +5 in steps of 0.05. If both are still 0 when you switch, the outlet
offset starts at the automatically measured correction.

**Affected:** Return/Leaving water temperature, Flow/return difference, Heat
output, COP, Heat produced since install (corrected) and the low-flow check.
Their "(reported)" twins always show the heat pump's own values. While the
compressor runs with less than 0.3 K reported difference (e.g. starting or
defrosting), Heat output and COP are briefly *unknown*.

**History.** Home Assistant's long-term statistics (the hourly data used by
history graphs for older periods, and statistics cards) can be recalculated
with the current calibration: go to **Developer tools → Actions**, run
`aurora_local.rebuild_history`, and tick *Return response* to see what was
imported. It recalculates from the raw archive (see below), so it needs
**Save raw data** switched on and only covers the days the archive keeps
(30 by default). It replaces the hourly statistics of the temperatures,
flow/return difference, heat output, COP (and their "(reported)" twins) and
the corrected heat counter; detailed recent history (the last 10 days, by
Home Assistant's default) isn't changed. You can run it again any time, for
example after changing the calibration. When an update changes how history
is calculated, a **repair** (Settings → Repairs) offers to run it; nothing
is rewritten until you confirm.

For the corrected heat counter, the rebuilt history is lined up with the
statistics Home Assistant already has, so energy graphs don't jump. Once the
archive no longer reaches back as far as those statistics (after its
retention period), that counter's history is left as it is.

## Data and privacy

- Everything stays on your network. The integration doesn't use the internet.
- **Raw archive.** With **Save raw data** on (default), every message from
  the heat pump is saved exactly as received in
  `config/aurora_local_raw/<device ID>/`, one file per day (UTC), compressed
  after the day ends (about 0.5 MB per day). It's used for the first
  calibration and for rebuilding history. Set how many days to keep
  (default 30, plus the current day) or turn it off under **Configure**.
  Turning it off doesn't delete existing files.
- Settings, calibration and counters are remembered in Home Assistant's
  storage folder (`.storage/aurora_local.*`).
- The raw archive contains your heat pump's device ID and detailed
  timestamps; don't share it publicly. **Download diagnostics** removes the
  device ID, product key and IP addresses, so it's safe to attach to a bug
  report.
- Port 8899 on the Wi-Fi module has no password: anyone on your network can
  read it. Consider putting the module on a separate IoT or guest network.

## Limitations

- Read-only: no control of the heat pump.
- Developed and tested on one heat pump. Some field meanings (water flow
  unit, most fault codes, many `paramN` fields) are still unknown.
- The automatic calibration needs the heat pump to stand still for 20 minutes
  or more now and then. If it never does, use manual calibration.

## Troubleshooting

| Problem | What to do |
|---|---|
| Setup fails | See the table under [Setup](#setup). |
| Entities *unavailable* | No data for 5 minutes. Check **Connection** and its *last error*; press **Reconnect now**. |
| COP *unknown* | Normal while the compressor is off or in standby (below 150 W), while it speeds up or slows down, or when it delivers no heat. |
| Heat produced since install is far below Heat produced, lifetime (reported) | Expected: one counts since this integration started and is corrected, the other since the heat pump was installed. |
| Heat output or COP look wrong | Check **Temperature calibration offset**. Switch to manual if needed, then run the rebuild action (see [History](#temperature-calibration)). |
| Quiet mode / zones / AHS / holiday *unknown* | They appear after the heat pump next reports them (see [First steps](#first-steps)). |
| Developer tools → Statistics lists issues for sensors that no longer exist | Left over from older versions; use *Fix issue* there to remove their old statistics if you don't need them. |

## Getting help

- Report problems at `https://github.com/lmbollen/aurora_local/issues`.
- Attach **Download diagnostics** (device page → ⋮); identifiers are
  already removed.
- For detailed logs, add this to `configuration.yaml` and restart:

  ```yaml
  logger:
    logs:
      custom_components.aurora_local: debug
  ```

## Updating and removing

- **Update:** in HACS, update the integration, then restart Home Assistant.
  If an update changes how history is calculated, a repair offers a rebuild.
- **Remove:** Settings → Devices & services → Aurora Local → ⋮ → Delete,
  then remove it in HACS (or delete `config/custom_components/aurora_local/`)
  and restart. The raw archive (`config/aurora_local_raw/`), the
  `.storage/aurora_local.*` file and the recorded statistics stay behind;
  delete the archive folder yourself if you don't need it.

## Glossary

| Term | Meaning |
|---|---|
| COP | Coefficient of performance: heat delivered per unit of electricity |
| DHW | Domestic hot water (the hot tap water tank) |
| AHS | Auxiliary heat source: the backup heater |
| K (kelvin) | Used here for temperature *differences*: 1 K = 1 °C difference |
| R290 | Propane, the refrigerant in the development unit |

## For developers

The heat pump code is a separate library, **pyauroralocal** (standard library
only, no Home Assistant imports), in
[`custom_components/aurora_local/pyauroralocal/`](custom_components/aurora_local/pyauroralocal/README.md).
It lives inside the integration folder so HACS installs need no download;
`pyproject.toml` builds it as a standalone package. The rest of
`custom_components/aurora_local/` is the Home Assistant layer.

The protocol, field meanings and the evidence behind them are in
[`docs/adlar-castra-protocol.md`](docs/adlar-castra-protocol.md) (developer
material). The `analysis/` folder contains the research scripts and replay
checks.

Development uses [Nix](https://nixos.org) for tools and
[uv](https://docs.astral.sh/uv/) for Python packages, both pinned
(`flake.lock`, `uv.lock`):

```bash
nix develop          # Python 3.14 (as Home Assistant uses) and uv
uv sync              # dev dependencies, incl. the Home Assistant test harness
uv run pytest        # library, offline and Home Assistant tests
uv run ruff check .
AURORA_ARCHIVE=/path/to/archive uv run pytest   # also replay a raw archive
uv build             # sdist and wheel of the library
```

CI runs the same commands, plus Home Assistant's `hassfest` and the HACS
validation.

## License

MIT, see [LICENSE](LICENSE). Not affiliated with, or endorsed by, Adlar, Castra or the makers of the Thermomax app.
