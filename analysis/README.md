# Analysis scripts and offline checks

Research scripts used to work out the protocol, and offline checks of the
library against a fake module or a recorded archive. They are not part of the
integration. `uv run pytest` (repository root) runs the checks marked below;
the archive replays only when `AURORA_ARCHIVE` points at an archive folder.

Most of them need a local copy of the integration's raw archive
(`config/aurora_local_raw/<device key>/`). That archive is personal data and
is **not** in this repository; copy your own into a folder outside the
repository and pass its path.

| Script | What it does | Run by pytest |
|---|---|---|
| `test_fixes.py`, `test_reconnect.py`, `test_rediscover.py` | Offline checks of the parser, client, calibration, low-flow logic and archive (fake modules on 127.0.0.x; no archive needed) | yes |
| `test_history.py`, `test_bootstrap.py`, `replay_calibration.py`, `replay_model.py`, `replay_events.py` | Replay an archive through the library and check the results | with `AURORA_ARCHIVE` |
| `check_consistency.py` | Checks that every field's numbers fit its meaning (32 physical and cross-field rules, e.g. suction ≥ evaporating, pressures equal at rest, fan 0 in defrost, `instantrate` = compressor COP × 100) on every burst | with `AURORA_ARCHIVE` |
| `build_timeseries.py` | Turn an archive into per-field time series (input for the next three) | no |
| `explore.py`, `hypotheses.py`, `flow_check.py` | The research that established field meanings and units; they take the output folder of `build_timeseries.py` and need pandas | no |
| `offset_study.py`, `crc_search.py` | How the sensor offset and the `crc` rule were found | no |
| `status.py`, `watch.py` | Summarise the tail of today's (uncompressed) archive file | no |

Usage is in the docstring at the top of each script. The first argument is
usually the library or integration folder:

```bash
uv run python analysis/test_fixes.py custom_components/aurora_local
uv run python analysis/replay_calibration.py custom_components/aurora_local /path/to/archive
uv run --with pandas python analysis/explore.py /path/to/build_timeseries/output
```

Times are printed in the Europe/Amsterdam time zone (where the development
unit is); change `TZ` at the top of a script for another zone.

These scripts only read files or talk to fake servers on `127.0.0.x`.
Nothing here sends data to a real heat pump.
