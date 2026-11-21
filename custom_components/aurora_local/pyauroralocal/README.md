# pyauroralocal

Local, read-only access to an Adlar Castra Aurora heat pump through its
Hi-Flying HF-LPT270 Wi-Fi module. Part of
[aurora_local](https://github.com/lmbollen/aurora_local); the Home Assistant
integration there is a thin layer over this library.

- Standard library only, Python 3.13+, asyncio, typed.
- **Never sends anything to the heat pump.** It only listens on TCP port 8899,
  where the module streams JSON. The command format is unknown, and a wrong
  write could change the pump's settings.
- Not affiliated with Adlar. Tested on a Castra Aurora III (7 kW, R290);
  other models with the same module probably work, but field meanings may
  differ.

## Install

Not on PyPI yet. Build it from the repository (`uv build`) or install from a
checkout: `pip install /path/to/aurora_local`.

## Example

```python
import asyncio
import time

from pyauroralocal import AdlarClient, BurstProcessor
from pyauroralocal import fields as F

processor = BurstProcessor()  # learns the temperature sensor offset


def on_burst(state):
    """Called once per complete measurement burst (about every 91 s)."""
    values = state.values
    corrected = processor.process(values, time.time(), time.monotonic())
    print(
        f"outdoor {F.temp(values, 'temp_envir2')} °C, "
        f"power {F.electrical_power(values)} W, "
        f"heat {corrected['heat']} W (reported {F.heat_output(values)} W), "
        f"COP {corrected['cop']}"
    )


async def main():
    client = AdlarClient("192.0.2.10", 8899, on_burst=on_burst)
    client.start()          # reconnects by itself, with backoff
    await asyncio.sleep(600)
    await client.stop()


asyncio.run(main())
```

`process()` returns the corrected values of the burst as a dict with
`temp_in`, `temp_out` (°C), `delta_t` (K), `heat` (W) and `cop` (any can be
None). Call it once per burst, as above; calling it more often would feed the
calibration duplicate samples.

### Callbacks

All run in the event loop; exceptions in them are logged, never raised into
the client.

| Callback | Called |
|---|---|
| `on_burst(state)` | once per complete measurement burst |
| `on_update(state, new_keys, new_tables)` (3rd positional argument) | after every read that changed values; `new_keys`/`new_tables` name fields seen for the first time |
| `on_status()` | when the connection status changes (`client.status`, `client.last_error`, ...) |
| `on_connection(connected)` | when the TCP connection opens or closes |
| `on_raw(text, message_or_None)` | for every message, for archiving |
| `on_host_change(new, old)` | when the module was found at a new address |

Pass `device_key=` to let the client find the module again when its IP
address changes: on the next failure after `reconnect_now()`, and with
`auto_rediscover=True` also automatically after repeated failures. It only
searches the /24 of a private IPv4 address, only connects and listens, and
recognises the module by its device key.

`RawLogger.flush`, `RawLogger.replay` and `history.build` do blocking file
I/O; run them in an executor from async code.

## Modules

| Module | Contents |
|---|---|
| `framing` | `StreamParser`: byte stream → JSON messages (resyncs after garbage) |
| `state` | `AdlarState`: merged values and tables, with size limits |
| `client` | `AdlarClient`: TCP client with exponential backoff (max 1 h) and rediscovery |
| `discovery` | `probe` a host, `find_module` on a subnet by device key |
| `fields` | Meaning and scaling of the fields: temperatures, power, heat, COP, faults, ... |
| `calibration` | `Calibration` of the water temperature sensors, `EnergyIntegrator` |
| `monitoring` | `LowFlowTracker`, `FaultTracker` |
| `processor` | `BurstProcessor`: one burst → corrected values; used live, on replay and for history |
| `archive` | `RawLogger`: archive of every message as received, with replay |
| `history` | Hourly history rebuilt from the archive |

The protocol and what each field means are described in
[docs/adlar-castra-protocol.md](https://github.com/lmbollen/aurora_local/blob/main/docs/adlar-castra-protocol.md).

## Temperature calibration

On the unit this was developed on, the outlet water sensor reads about
0.86 K high. With a ΔT of 3–5 K that makes the pump's own heat output (and
so its COP) 20–40 % too high. `Calibration` learns the offset from periods
when the compressor has been off for 20 minutes and no water flows (both
sensors should then read the same), and `BurstProcessor` scales the heat
output with the corrected ΔT. Manual offsets are possible too.
