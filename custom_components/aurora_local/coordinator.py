"""Push coordinator: the heat pump sends data, we never poll the device."""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification as pn
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_NOTIFY,
    CONF_RAW_LOG,
    CONF_RETENTION_DAYS,
    CONF_SCAN,
    DEFAULT_NOTIFY,
    DEFAULT_RAW_LOG,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SCAN,
    DOMAIN,
    HISTORY_MAX_RETRIES,
    HISTORY_RETRY_SECONDS,
    HISTORY_VERSION,
    ISSUE_MOVED,
    RAW_FLUSH_SECONDS,
    RAW_LOG_DIR,
    RECENT_RAW,
    SIGNAL_NEW_KEYS,
    SIGNAL_NEW_TABLES,
    STORE_VERSION,
)
from .pyauroralocal import (
    CAL_AUTO,
    CAL_MANUAL,
    AdlarClient,
    AdlarState,
    BurstProcessor,
    FaultTracker,
    RawLogger,
)
from .pyauroralocal.calibration import Calibration, DailyEnergy, EnergyIntegrator, energy_cop
from .pyauroralocal.client import STALE_AFTER
from .pyauroralocal.fields import CONFIRMED_CODES, counter_kwh
from .pyauroralocal.monitoring import LOW_FLOW_HOLD_SECONDS, FaultChange, LowFlowTracker

_LOGGER = logging.getLogger(__name__)
SAVE_DELAY = 300  # seconds; worst-case data loss on a crash
SAVE_SOON = 5
# After midnight, wait this long before rewriting the finished day's hourly
# COP statistics, so the recorder has compiled the day's last hour first.
DAILY_WRITE_DELAY = 15 * 60


def local_day(ts: float) -> str:
    """Local (Home Assistant time zone) date of an epoch, as YYYY-MM-DD."""
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).date().isoformat()

FAULT_HINTS = {
    "P01": "Water flow protection (seen on this unit when flow collapsed). "
           "Check the water filter, system pressure, air in the system and closed valves.",
}


def _local(ts: float | None) -> str:
    if ts is None:
        return "unknown"
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).strftime("%Y-%m-%d %H:%M")


class AdlarCoordinator(DataUpdateCoordinator[AdlarState]):
    """Owns the client, the burst processing, persistence and alerts.

    Entities are updated once per measurement burst (~91 s) and on
    connection status changes. The 60 s interval only re-evaluates
    availability, the low-flow hold and the offline alert; it never talks to
    the device.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, host: str, port: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=60),
        )
        self.client = AdlarClient(
            host, port, self._handle_update,
            on_burst=self._handle_burst, on_raw=self._handle_raw, on_status=self._handle_status,
            on_host_change=self._handle_host_change, device_key=entry.unique_id,
            auto_rediscover=entry.options.get(CONF_SCAN, DEFAULT_SCAN),
        )
        self.options_snapshot = dict(entry.options)
        self.data = self.client.state
        self.recent_raw: deque[dict[str, Any]] = deque(maxlen=RECENT_RAW)
        self.seen_keys: set[str] = set()
        self.seen_tables: set[str] = set()
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._unsub_flush: Any = None
        self.processor = BurstProcessor()  # calibration, calibrated heat, low flow
        self.daily = DailyEnergy()  # heat and electricity per local day (COP today)
        self._daily_pending: tuple[str, float, float] | None = None  # day to write to statistics
        self._daily_write: Any = None
        self._low_flow_was_active = False
        self.faults = FaultTracker()
        self.history_version = 0
        self.migrations: set[str] = set()  # one-time migrations already done
        self._save_handle: Any = None
        self._save_due = 0.0
        self._history_retry: Any = None
        self._history_retries = 0
        self._offline_notified = False
        self._unsub_ha_stop: Any = None
        self._stopped = False
        self._started_at = time.monotonic()
        self.notify = entry.options.get(CONF_NOTIFY, DEFAULT_NOTIFY)
        self._nid = f"{DOMAIN}_{entry.entry_id}"

        raw_enabled = entry.options.get(CONF_RAW_LOG, DEFAULT_RAW_LOG)
        retention = entry.options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)
        self.raw_logger: RawLogger | None = (
            RawLogger(Path(hass.config.path(RAW_LOG_DIR, entry.unique_id or entry.entry_id)), retention)
            if raw_enabled
            else None
        )

    # -- lifecycle -------------------------------------------------------

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self._restore(stored)
        if self.raw_logger:
            await self._async_restore_from_archive(has_calibration=bool(stored.get("calibration")))
        if self.raw_logger:
            self._unsub_flush = async_track_time_interval(
                self.hass, self._async_flush_raw, timedelta(seconds=RAW_FLUSH_SECONDS)
            )
        # Home Assistant doesn't unload integrations when it stops, so save
        # and flush on the stop event too (otherwise up to 5 min is lost).
        self._unsub_ha_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_on_ha_stop
        )
        self._started_at = time.monotonic()
        if self._daily_pending is not None:  # a finished day not yet written (restart after midnight)
            self._schedule_daily_write()
        self.client.start()

    def _restore(self, stored: dict[str, Any]) -> None:
        """Restore the stored snapshot. Every part is optional and validated:
        a damaged store must not stop the integration from starting."""
        self.seen_keys = {k for k in stored.get("keys", []) if isinstance(k, str)}
        self.seen_tables = {t for t in stored.get("tables", []) if isinstance(t, str)}
        self.calibration.load(stored.get("calibration"))
        try:
            self.heat_energy.kwh = max(0.0, float(stored.get("heat_calibrated_kwh", 0.0)))
        except (TypeError, ValueError):
            self.heat_energy.kwh = 0.0
        # Last known values: since a power cycle the pump only sends settings
        # (quiet mode, zones, AHS, holiday) when they change, so without this
        # they'd stay unknown after a restart. Entities stay unavailable until
        # fresh data arrives, and any newer report overwrites these.
        restored = stored.get("values")
        if isinstance(restored, dict):
            self.client.state.apply({"method": "restore", "params": restored})
        for name, table in (stored.get("table_values") or {}).items():
            try:
                self.client.state.tables[str(name)] = {int(k): v for k, v in table.items()}
            except (AttributeError, TypeError, ValueError):
                continue
        self.client.state.last_update = None  # restored values are not fresh
        stored_codes = stored.get("fault_codes")
        self.faults = FaultTracker(
            [c for c in stored_codes if isinstance(c, str)] if isinstance(stored_codes, list) else None
        )
        try:
            self.history_version = int(stored.get("history_version", 0))
        except (TypeError, ValueError):
            self.history_version = 0
        self.migrations = {m for m in stored.get("migrations", []) if isinstance(m, str)}
        self.daily.load(stored.get("daily"))
        pending = stored.get("daily_pending")
        if (isinstance(pending, list) and len(pending) == 3 and isinstance(pending[0], str)
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in pending[1:])):
            self._daily_pending = (pending[0], float(pending[1]), float(pending[2]))

    async def _async_restore_from_archive(self, has_calibration: bool) -> None:
        """Apply the archive's newest values on top of the snapshot (the archive
        is flushed every 30 s, the snapshot only every few minutes). On the
        first run, also learn the calibration from the whole archive."""
        assert self.raw_logger is not None
        try:
            if has_calibration:
                await self.hass.async_add_executor_job(self.raw_logger.replay, 2, None, self.client.state)
            else:
                await self.hass.async_add_executor_job(self._bootstrap_from_archive)
                _LOGGER.info(
                    "Calibration learned from the archive: offset %s K from %d samples, "
                    "calibrated heat %.1f kWh",
                    self.calibration.auto_offset(time.time()), len(self.calibration.samples),
                    self.heat_energy.kwh,
                )
        except Exception:  # restoring is best effort
            _LOGGER.warning("Could not restore values from the raw archive", exc_info=True)
        self.client.state.last_update = None

    def _bootstrap_from_archive(self) -> None:
        """Blocking (executor). Learn the calibration from the archive, then
        integrate the archive's heat with the learned offsets, the same way a
        history rebuild does, so the live counter and rebuilt history agree."""
        assert self.raw_logger is not None
        self.raw_logger.replay(None, self.processor.process, self.client.state)
        inlet, outlet = self.calibration.offsets(time.time())
        fixed = BurstProcessor.fixed(inlet, outlet)
        self.raw_logger.replay(None, fixed.process)
        self.heat_energy.kwh = fixed.energy.kwh

    async def _async_on_ha_stop(self, _event: Any) -> None:
        self._unsub_ha_stop = None
        await self.async_stop()

    async def async_stop(self) -> None:
        """Stop the client, flush the archive and save. Safe to call twice
        (Home Assistant stop event, then unload)."""
        if self._stopped:
            return
        self._stopped = True
        if self._unsub_ha_stop is not None:
            self._unsub_ha_stop()
            self._unsub_ha_stop = None
        await self.client.stop()
        if self._unsub_flush:
            self._unsub_flush()
            self._unsub_flush = None
        await self._async_flush_raw()
        for attr in ("_save_handle", "_history_retry", "_daily_write"):
            handle = getattr(self, attr)
            if handle is not None:
                handle()
                setattr(self, attr, None)
        await self._store.async_save(self._store_data())

    @callback
    def async_reconnect(self) -> None:
        self.client.reconnect_now()
        self.async_update_listeners()

    @property
    def calibration(self) -> Calibration:
        return self.processor.calibration

    @property
    def heat_energy(self) -> EnergyIntegrator:
        return self.processor.energy

    @property
    def low_flow(self) -> LowFlowTracker:
        return self.processor.low_flow

    @property
    def corrected(self) -> dict[str, float | None]:
        """Calibrated values of the latest burst (temp_in, temp_out, delta_t, heat, cop)."""
        return self.processor.corrected

    @property
    def fresh(self) -> bool:
        age = self.client.state.age()
        return age is not None and age < STALE_AFTER

    async def _async_update_data(self) -> AdlarState:
        self._check_offline()
        self._check_low_flow()
        return self.client.state

    # -- persistence -----------------------------------------------------

    def _store_data(self) -> dict[str, Any]:
        return {
            "keys": sorted(self.seen_keys),
            "tables": sorted(self.seen_tables),
            "values": dict(self.client.state.values),
            "table_values": {n: dict(t) for n, t in self.client.state.tables.items()},
            "fault_codes": self.faults.last_known,
            "calibration": self.calibration.to_dict(),
            "heat_calibrated_kwh": round(self.heat_energy.kwh, 4),
            "history_version": self.history_version,
            "migrations": sorted(self.migrations),
            "daily": self.daily.to_dict(),
            "daily_pending": list(self._daily_pending) if self._daily_pending else None,
        }

    @callback
    def request_save(self, delay: float = SAVE_DELAY) -> None:
        """Save within `delay` seconds.

        Unlike Store.async_delay_save, later calls never postpone a pending
        save (data arrives more often than once a minute, so a debounced save
        would never happen); a shorter delay brings a pending save forward.
        """
        due = time.monotonic() + delay
        if self._save_handle is not None:
            if due >= self._save_due:
                return
            self._save_handle()
        self._save_due = due
        self._save_handle = async_call_later(self.hass, delay, self._async_save_now)

    async def _async_save_now(self, _now: datetime | None = None) -> None:
        self._save_handle = None
        await self._store.async_save(self._store_data())

    # -- history ---------------------------------------------------------

    @callback
    def _schedule_daily_write(self) -> None:
        """Rewrite the finished day's hourly "COP today" statistics with the
        day's energy-based COP (see history_import.async_write_daily_cop)."""
        if self._daily_write is not None or self._stopped:
            return

        async def _write(_now: datetime) -> None:
            self._daily_write = None
            pending = self._daily_pending
            if pending is None or self._stopped:
                return
            cop = energy_cop(pending[1], pending[2])
            if cop is not None:
                from .history_import import async_write_daily_cop  # needs recorder; import lazily

                try:
                    rows = await async_write_daily_cop(self, pending[0], cop)
                except Exception:  # keep it pending; retried at the next start
                    _LOGGER.warning("Could not write the daily COP for %s", pending[0], exc_info=True)
                    return
                _LOGGER.info("Daily COP %s: %.2f (%.2f kWh heat / %.2f kWh electricity), %d hours written",
                             pending[0], cop, pending[1], pending[2], rows)
            self._daily_pending = None
            self.request_save(SAVE_SOON)

        self._daily_write = async_call_later(self.hass, DAILY_WRITE_DELAY, _write)

    @callback
    def schedule_energy_history_retry(self) -> None:
        """Retry the history import once the recorder has 5-minute statistics
        for the calibrated heat counter (needed to align the imported sums)."""
        if self._history_retry is not None or self._history_retries >= HISTORY_MAX_RETRIES:
            return
        self._history_retries += 1

        async def _retry(_now: datetime) -> None:
            self._history_retry = None
            try:
                await self.async_rebuild_history()
            except Exception:
                _LOGGER.exception("History rebuild retry failed")

        self._history_retry = async_call_later(self.hass, HISTORY_RETRY_SECONDS, _retry)

    async def async_rebuild_history(self) -> dict[str, Any]:
        """Rewrite long-term statistics from the raw archive (see history_import)."""
        from .history_import import async_rebuild_history  # needs recorder; import lazily

        result = await async_rebuild_history(self)
        if not str(result.get("heat_counter", "")).startswith("deferred"):
            # Only count the rebuild as done once the heat counter part ran.
            self.history_version = HISTORY_VERSION
            self._history_retries = 0
        if not self._stopped:
            self.request_save(SAVE_SOON)
            self.async_update_listeners()
        return result

    # -- calibration -----------------------------------------------------

    @callback
    def async_set_calibration_mode(self, mode: str) -> None:
        if mode not in (CAL_AUTO, CAL_MANUAL) or mode == self.calibration.mode:
            return
        cal = self.calibration
        if mode == CAL_MANUAL and cal.manual_inlet == 0 and cal.manual_outlet == 0:
            # Start manual mode from the learned values so nothing jumps.
            auto = cal.auto_offset(time.time())
            if auto is not None:
                cal.manual_outlet = -auto
        cal.mode = mode
        _LOGGER.info("Temperature calibration set to %s", mode)
        self._calibration_changed()

    @callback
    def async_set_manual_offset(self, sensor: str, value: float) -> None:
        """Setting an offset switches to manual calibration (it has no effect in auto)."""
        cal = self.calibration
        if cal.mode != CAL_MANUAL:
            if cal.manual_inlet == 0 and cal.manual_outlet == 0:
                auto = cal.auto_offset(time.time())
                if auto is not None:
                    cal.manual_outlet = -auto
            cal.mode = CAL_MANUAL
            _LOGGER.info("Temperature calibration set to manual (offset changed)")
        setattr(cal, f"manual_{sensor}", round(float(value), 2))
        self._calibration_changed()

    def _calibration_changed(self) -> None:
        self.processor.recompute(self.client.state.values, time.time())
        self.request_save(SAVE_SOON)
        self.async_update_listeners()

    # -- raw archive -----------------------------------------------------

    async def _async_flush_raw(self, _now: datetime | None = None) -> None:
        if not self.raw_logger:
            return
        lines = self.raw_logger.take()
        try:
            await self.hass.async_add_executor_job(self.raw_logger.flush, lines)
        except OSError as err:
            _LOGGER.warning("Could not write raw data log: %s", err)

    @callback
    def _handle_raw(self, raw: str, msg: dict[str, Any] | None) -> None:
        self.recent_raw.append(
            {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "raw": raw, "ok": msg is not None}
        )
        if self.raw_logger:
            self.raw_logger.add(raw, msg is not None)
        if msg is None:
            _LOGGER.debug("Received non-JSON data: %r", raw[:200])

    # -- updates ---------------------------------------------------------

    @callback
    def _handle_host_change(self, new_host: str, old_host: str) -> None:
        """The module answered with our device key at a new address."""
        entry = self.config_entry
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: new_host})
        self.hass.bus.async_fire(f"{DOMAIN}_host_changed", {"old": old_host, "new": new_host})
        ir.async_create_issue(
            self.hass, DOMAIN, f"{ISSUE_MOVED}_{entry.entry_id}",
            is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_MOVED, translation_placeholders={"old": old_host, "new": new_host},
        )

    @callback
    def _handle_status(self) -> None:
        self._check_offline()
        self.async_update_listeners()

    @callback
    def _handle_update(self, state: AdlarState, new_keys: set[str], new_tables: set[str]) -> None:
        """Every read with values: only add entities for new fields here."""
        # Compare against everything known, not just what the parser reports
        # as new: values restored from the archive (e.g. after re-adding the
        # integration) are never "new" to the parser but still need entities.
        new_keys = (new_keys | set(state.values)) - self.seen_keys
        new_tables = (new_tables | set(state.tables)) - self.seen_tables
        if not (new_keys or new_tables):
            return
        if self.seen_keys or self.seen_tables:
            _LOGGER.info(
                "Heat pump reported new fields %s / tables %s; adding entities",
                sorted(new_keys), sorted(new_tables),
            )
        self.seen_keys |= new_keys
        self.seen_tables |= new_tables
        self.request_save(10)
        entry_id = self.config_entry.entry_id
        if new_keys:
            async_dispatcher_send(self.hass, SIGNAL_NEW_KEYS.format(entry_id), new_keys)
        if new_tables:
            async_dispatcher_send(self.hass, SIGNAL_NEW_TABLES.format(entry_id), new_tables)

    @callback
    def _handle_burst(self, state: AdlarState) -> None:
        """Once per measurement burst: derived values, alerts, entity update."""
        now = time.time()
        self.processor.process(state.values, now, time.monotonic())
        done = self.daily.add(local_day(now), now, self.heat_energy.kwh, counter_kwh(state.values, "power_use"))
        if done is not None:
            self._daily_pending = done
            self._schedule_daily_write()
        self._check_faults(state)
        self._check_low_flow()
        self._check_offline()
        self.request_save()
        self.async_set_updated_data(state)

    # -- alerts ----------------------------------------------------------

    def _notify(self, suffix: str, title: str, message: str) -> None:
        if self.notify:
            pn.async_create(self.hass, message, title=title, notification_id=f"{self._nid}_{suffix}")

    def _dismiss(self, suffix: str) -> None:
        pn.async_dismiss(self.hass, f"{self._nid}_{suffix}")

    @callback
    def _check_offline(self) -> None:
        """Alert when no data for STALE_AFTER; clear it when data flows again."""
        client = self.client
        if self.fresh:
            if self._offline_notified:
                self._offline_notified = False
                self._dismiss("offline")
                self.hass.bus.async_fire(f"{DOMAIN}_connection", {"connected": True})
                _LOGGER.info("Heat pump data is flowing again")
            return
        # Not fresh: only alert once we've been running long enough to expect data.
        if self._offline_notified or time.monotonic() - self._started_at < STALE_AFTER:
            return
        self._offline_notified = True
        last = client.state.last_update
        since = (
            _local(time.time() - (time.monotonic() - last)) if last is not None else "before the last restart"
        )
        msg = (
            f"No data from the heat pump's Wi-Fi module ({client.host}:{client.port}) since {since}.\n\n"
            f"Last error: {client.last_error or 'none (connected but silent)'}\n"
            f"Failed attempts: {client.failures}. Retries back off up to once an hour; "
            "press **Reconnect now** on the device page to retry immediately.\n\n"
            "If the heat pump itself shows a fault, check its display."
        )
        self._notify("offline", "Heat pump offline", msg)
        self.hass.bus.async_fire(
            f"{DOMAIN}_connection",
            {"connected": False, "last_error": client.last_error, "failures": client.failures},
        )
        _LOGGER.warning("No data from heat pump for over %d minutes", STALE_AFTER // 60)

    @callback
    def _check_faults(self, state: AdlarState) -> None:
        """Fire `aurora_local_fault` and notify when the fault codes change."""
        change: FaultChange | None = self.faults.update(state.values)
        if change is None:
            return
        if change.still_active:
            # Unchanged since before the restart: notifications don't survive
            # restarts, so show it again (no new event).
            self._notify_faults(change.codes, still_active=True)
            return
        self.request_save(10)
        self.hass.bus.async_fire(
            f"{DOMAIN}_fault",
            {"device_key": state.device_key, "codes": change.codes, "previous": change.previous,
             "new": change.new, "cleared": change.cleared},
        )
        _LOGGER.warning("Heat pump fault codes changed: %s -> %s", change.previous, change.codes)
        if change.codes:
            self._notify_faults(change.codes, cleared=change.cleared)
        else:
            self._dismiss("fault")

    def _notify_faults(self, codes: list[str], cleared: list[str] | None = None,
                       still_active: bool = False) -> None:
        """One notification listing all active codes (replaced on every change,
        removed when all have cleared)."""
        now = dt_util.now().strftime("%H:%M")
        head = "still active after restart" if still_active else f"as of {now}"
        lines = [f"Active fault{'s' if len(codes) > 1 else ''} **{', '.join(codes)}** ({head})."]
        if cleared:
            lines.append(f"Cleared: {', '.join(cleared)}.")
        for c in codes:
            hint = FAULT_HINTS.get(c)
            if hint:
                lines.append(f"- {c}: {hint}")
            elif c not in CONFIRMED_CODES:
                lines.append(f"- {c}: code number inferred from the fault word; check the display.")
        self._notify("fault", "Heat pump fault", "\n".join(lines))

    @callback
    def _check_low_flow(self) -> None:
        active = self.low_flow.active()
        if active == self._low_flow_was_active:
            return
        self._low_flow_was_active = active
        self.hass.bus.async_fire(f"{DOMAIN}_low_flow", {"active": active, "reason": self.low_flow.reason})
        if active:
            self._notify(
                "low_flow", "Heat pump low water flow",
                f"Since {dt_util.now().strftime('%H:%M')}, water flow has been low for at least "
                f"{LOW_FLOW_HOLD_SECONDS // 60} minutes while the compressor runs ({self.low_flow.reason}). "
                "Low flow can lead to a flow fault (such as P01). Check the water filter, system "
                "pressure, air in the system and closed valves. This notification disappears when "
                "flow is back to normal.",
            )
        else:
            self._dismiss("low_flow")
