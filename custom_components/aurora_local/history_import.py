"""Backfill Home Assistant long-term statistics from the raw archive.

Uses the recorder's official import API (async_import_statistics), which
replaces rows for the same hour; nothing in the database is edited directly.
Corrected values go to the normal sensors, the pump's own values to the
"(reported)" twins, and the calibrated heat counter gets its full history.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models.statistics import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_short_term_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
    TemperatureDeltaConverter,
)

from .const import DOMAIN
from .pyauroralocal import history

if TYPE_CHECKING:
    from .coordinator import AdlarCoordinator

_LOGGER = logging.getLogger(__name__)

# Native unit and converter per imported series (values are computed in these units).
UNITS: dict[str, tuple[str | None, type[BaseUnitConverter] | None]] = {
    "temp_in": (UnitOfTemperature.CELSIUS, TemperatureConverter),
    "temp_out": (UnitOfTemperature.CELSIUS, TemperatureConverter),
    "temp_in_reported": (UnitOfTemperature.CELSIUS, TemperatureConverter),
    "temp_out_reported": (UnitOfTemperature.CELSIUS, TemperatureConverter),
    "delta_t": (UnitOfTemperature.KELVIN, TemperatureDeltaConverter),
    "delta_t_reported": (UnitOfTemperature.KELVIN, TemperatureDeltaConverter),
    "heat_output": (UnitOfPower.WATT, PowerConverter),
    "heat_output_reported": (UnitOfPower.WATT, PowerConverter),
    "cop": (None, None),
    "cop_reported": (None, None),
    history.ENERGY_KEY: (UnitOfEnergy.KILO_WATT_HOUR, EnergyConverter),
}


def _converter(key: str, target: str | None) -> Callable[[float], float]:
    """Function converting a value from the native unit to the stored unit."""
    native, conv = UNITS.get(key, (None, None))
    if not native or not target or native == target or conv is None:
        return lambda v: v
    if target in conv.VALID_UNITS:
        return lambda v: conv.convert(v, native, target)
    _LOGGER.warning("Can't convert %s from %s to %s; importing unconverted", key, native, target)
    return lambda v: v


async def _align_energy(
    coordinator: AdlarCoordinator, entity_id: str, key_rows: list[dict[str, float]], total: float
) -> tuple[list[dict[str, float]], str]:
    """Shift the archive's heat counter rows onto the recorder's running sum.

    The recorder continues each hour's `sum` from the previous row, so imported
    rows must join the rows it already has. Rows: {start, state, sum} with
    state = sum = energy since the start of the archive, already in the
    recorder's unit; `total` is the archive's value now.

    - Recorder has hourly rows inside the imported range: anchor on the newest
      one, so the boundary with the rows the recorder keeps compiling is exact.
    - Recorder has rows *older* than the archive (archive pruned after its
      retention period): skip, because the older rows can't be rewritten and a
      step would appear where the archive starts.
    - Recorder has no hourly rows yet (first install): anchor the archive's
      current total on its latest 5-minute statistic; if there are none yet,
      retry later.
    """
    hass = coordinator.hass
    first, last = key_rows[0]["start"], key_rows[-1]["start"]
    hourly = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(first - 86400 * 400), None,
        {entity_id}, "hour", None, {"state", "sum"},
    )
    rec = [r for r in hourly.get(entity_id, []) if r.get("sum") is not None and r.get("state") is not None]
    by_start = {float(r["start"]): r for r in rec}
    archive = {r["start"]: r for r in key_rows}

    if any(float(r["start"]) < first for r in rec):
        return [], ("skipped: the recorder has heat counter history older than the archive "
                    "(archive retention); importing would create a step")

    common = sorted(s for s in by_start if s in archive and s <= last)
    if common:
        anchor = common[-1]
        sum_shift = float(by_start[anchor]["sum"]) - archive[anchor]["sum"]
        state_shift = float(by_start[anchor]["state"]) - archive[anchor]["state"]
        note = f"aligned to recorder hour {dt_util.utc_from_timestamp(anchor).isoformat()}"
        keep = [r for r in key_rows if r["start"] <= anchor]
    else:
        last_st = await get_instance(hass).async_add_executor_job(
            get_last_short_term_statistics, hass, 1, entity_id, False, {"state", "sum"}
        )
        st = (last_st.get(entity_id) or [None])[0]
        if not st or st.get("state") is None or st.get("sum") is None:
            coordinator.schedule_energy_history_retry()
            return [], "deferred: no recorder statistics yet, retrying later"
        # The latest 5-minute statistic is (practically) now, where the archive
        # stands at `total`: map that point onto the recorder's state and sum.
        sum_shift = float(st["sum"]) - total
        state_shift = float(st["state"]) - total
        note = "aligned to recorder 5-minute statistics"
        keep = key_rows
    return [
        {"start": r["start"], "state": round(r["state"] + state_shift, 4), "sum": round(r["sum"] + sum_shift, 4)}
        for r in keep
    ], note


def _metadata(key: str, entity_id: str, existing: dict[str, Any]) -> StatisticMetaData:
    if entity_id in existing:
        meta: StatisticMetaData = dict(existing[entity_id][1])  # type: ignore[assignment]
        meta.pop("has_mean", None)
        return meta
    unit, conv = UNITS.get(key, (None, None))
    is_energy = key == history.ENERGY_KEY
    return {
        "mean_type": StatisticMeanType.NONE if is_energy else StatisticMeanType.ARITHMETIC,
        "has_sum": is_energy,
        "name": None,
        "source": "recorder",
        "statistic_id": entity_id,
        "unit_class": conv.UNIT_CLASS if conv else None,
        "unit_of_measurement": unit,
    }


async def async_rebuild_history(coordinator: AdlarCoordinator) -> dict[str, Any]:
    hass = coordinator.hass
    if coordinator.raw_logger is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_archive")
    started = time.monotonic()
    inlet, outlet = coordinator.calibration.offsets(time.time())
    current_hour = int(time.time() // 3600 * 3600)
    rows, total_kwh = await hass.async_add_executor_job(
        history.build, coordinator.raw_logger, inlet, outlet, current_hour
    )

    registry = er.async_get(hass)
    uid = coordinator.config_entry.unique_id
    ids = {key: registry.async_get_entity_id("sensor", DOMAIN, f"{uid}_{key}") for key in rows}
    ids = {k: v for k, v in ids.items() if v}
    existing = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids=set(ids.values()))
    )

    summary: dict[str, int] = {}
    energy_note = "not imported"
    for key, entity_id in ids.items():
        if not rows.get(key):
            continue
        meta = _metadata(key, entity_id, existing)
        # Values are computed in native units (°C, K, W, kWh); the recorder may
        # store this sensor in another unit (e.g. °F or MWh if the user changed
        # the display unit), so convert into the metadata's unit first.
        convert = _converter(key, meta.get("unit_of_measurement"))
        key_rows = [{k: (v if k == "start" else convert(v)) for k, v in r.items()} for r in rows[key]]
        is_energy = key == history.ENERGY_KEY
        if is_energy:
            key_rows, energy_note = await _align_energy(coordinator, entity_id, key_rows, convert(total_kwh))
            if not key_rows:
                continue
        fields = ("state", "sum") if is_energy else ("mean", "min", "max")
        stats: list[StatisticData] = [
            {"start": dt_util.utc_from_timestamp(r["start"]), **{f: r[f] for f in fields}}  # type: ignore[typeddict-item]
            for r in key_rows
        ]
        async_import_statistics(hass, meta, stats)
        summary[entity_id] = len(stats)

    # The live counter is deliberately left alone: lowering it would look like
    # a meter reset to the recorder.
    result = {
        "offsets": {"inlet": inlet, "outlet": outlet},
        "hours_imported": summary,
        "heat_counter": energy_note,
        "calibrated_heat_kwh_in_archive": round(total_kwh, 2),
        "seconds": round(time.monotonic() - started, 1),
    }
    _LOGGER.info("Rebuilt heat pump history from the raw archive: %s", result)
    return result
