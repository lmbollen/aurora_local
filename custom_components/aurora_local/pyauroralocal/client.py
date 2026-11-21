"""Read-only TCP client for the HF-LPT270 module (port 8899).

It never writes to the socket: anything sent to port 8899 would go to the
heat pump controller, and the command format is unknown."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import time
from collections.abc import Callable
from typing import Any

from .discovery import LISTEN_TIMEOUT, find_module, subnet_candidates
from .framing import StreamParser
from .state import AdlarState

_LOGGER = logging.getLogger(__name__)

RawCallback = Callable[[str, dict[str, Any] | None], None]
UpdateCallback = Callable[[AdlarState, set[str], set[str]], None]
BurstCallback = Callable[[AdlarState], None]

# Observed behaviour of the HF-LPT270 / heat pump:
# - a full data burst (~38 messages) roughly every 91 s, with a "fixtime"
#   keep-alive in between;
# - the module drops listen-only clients after ~300 s (its default TCP
#   timeout), so a clean close is expected and we reconnect right away.
IDLE_TIMEOUT = 150.0
STALE_AFTER = 300.0
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_WAITING = "waiting_to_retry"
STATUS_STOPPED = "stopped"
STATUS_SEARCHING = "searching"
STATUSES = [STATUS_CONNECTING, STATUS_CONNECTED, STATUS_WAITING, STATUS_SEARCHING, STATUS_STOPPED]
REDISCOVER_AFTER = 3          # failed attempts before an automatic subnet scan
REDISCOVER_INTERVAL = 600.0   # at most one automatic scan per 10 minutes
BACKOFF_BASE = 5.0       # first retry after a failure
BACKOFF_MAX = 3600.0     # never wait longer than 1 hour
BACKOFF_JITTER = 0.1     # ±10 % so retries don't synchronise
CONNECT_TIMEOUT = 10.0
CLEAN_RECONNECT_DELAY = 1.0  # after the module's routine 5-minute close


def _describe(err: BaseException) -> str:
    text = str(err)
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


def _is_private_ipv4(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # a hostname: nothing to scan
    return addr.version == 4 and addr.is_private


def _fmt_delay(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


class AdlarClient:
    """Keeps a TCP connection open and calls back on every update.

    Reconnects forever with exponential backoff (5 s doubling up to 1 h, ±10 %
    jitter). A connection only counts as healthy once data has been received;
    the module's routine close after ~5 min reconnects after 1 s without
    backing off. `reconnect_now()` skips the wait and resets the backoff.
    The loop never exits on an unexpected error, and restarts itself if it
    somehow ends.
    """

    def __init__(
        self,
        host: str,
        port: int,
        on_update: UpdateCallback | None = None,
        *,
        on_burst: BurstCallback | None = None,
        on_connection: Callable[[bool], None] | None = None,
        on_raw: RawCallback | None = None,
        on_status: Callable[[], None] | None = None,
        on_host_change: Callable[[str, str], None] | None = None,
        device_key: str | None = None,
        auto_rediscover: bool = False,
        idle_timeout: float = IDLE_TIMEOUT,
        backoff_base: float = BACKOFF_BASE,
        backoff_max: float = BACKOFF_MAX,
        connect_timeout: float = CONNECT_TIMEOUT,
        clean_reconnect_delay: float = CLEAN_RECONNECT_DELAY,
        rediscover_after: int = REDISCOVER_AFTER,
        rediscover_interval: float = REDISCOVER_INTERVAL,
        scan_listen_timeout: float = LISTEN_TIMEOUT,
    ) -> None:
        """Callbacks run in the event loop:

        - on_update(state, new_keys, new_tables): after every read that changed values
        - on_burst(state): once per completed measurement burst (~91 s); the
          place to compute derived values (see BurstProcessor)
        - on_connection(connected), on_status(): connection changes
        - on_raw(text, message_or_None): every message, for archiving
        - on_host_change(new, old): the module was found at a new address

        With `device_key` set, the client can search the local /24 subnet for
        the module after an IP change: automatically after repeated failures
        if `auto_rediscover` is True, and on the next failure after
        `reconnect_now()`. Only private IPv4 addresses are ever scanned.
        """
        self.host = host
        self.port = port
        self.device_key = device_key
        self._on_host_change = on_host_change
        self._auto_rediscover = auto_rediscover
        self._rediscover_after = rediscover_after
        self._rediscover_interval = rediscover_interval
        self._scan_listen_timeout = scan_listen_timeout
        self._last_scan: float | None = None
        self._scan_requested = False
        self.last_scan_result: str | None = None
        self.state = AdlarState()
        self._on_update = on_update
        self._on_burst = on_burst
        self._clean_reconnect_delay = clean_reconnect_delay
        self._on_connection = on_connection
        self._on_status = on_status
        self._on_raw = on_raw
        self._idle_timeout = idle_timeout
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._connect_timeout = connect_timeout
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False  # set by stop(); the client never restarts after that
        self._restart_handle: asyncio.TimerHandle | None = None
        self._routine = False  # last session ended with the module's routine close
        self._parser = StreamParser()
        self.connected = False
        # Status for the UI
        self.status = STATUS_STOPPED
        self.failures = 0               # consecutive failed attempts
        self.last_error: str | None = None
        self.next_attempt: float | None = None   # time.time() of next try
        self.last_connected: float | None = None  # time.time()
        self.last_disconnected: float | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        # Once stopped (integration unloaded), never come back: neither a
        # pending self-restart nor a reconnect request may revive the loop.
        if self._closed:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())
            self._task.add_done_callback(self._task_done)

    async def stop(self) -> None:
        self._closed = True
        if self._restart_handle is not None:
            self._restart_handle.cancel()
            self._restart_handle = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._set_status(STATUS_STOPPED)

    def reconnect_now(self) -> None:
        """Retry immediately (or force a fresh connection) and reset backoff."""
        if self._closed:
            return
        _LOGGER.info("Reconnect to %s:%s requested", self.host, self.port)
        if self.failures:
            self._scan_requested = True  # if this attempt fails too, search the subnet
        self.failures = 0
        if self._writer is not None:
            self._writer.close()  # read loop ends, then reconnects at once
        self._wake.set()
        self.start()  # in case the loop isn't running

    def _task_done(self, task: asyncio.Task) -> None:
        if self._closed or task.cancelled():
            return
        exc = task.exception()
        _LOGGER.error("Connection loop ended unexpectedly (%s); restarting", exc and _describe(exc))
        self._task = None
        self._restart_handle = asyncio.get_running_loop().call_later(self._backoff_base, self._restart)

    def _restart(self) -> None:
        self._restart_handle = None
        self.start()

    # -- status ----------------------------------------------------------

    def _set_status(self, status: str) -> None:
        self.status = status
        self._notify_status()  # even if unchanged: next_attempt etc. may have changed

    def _notify_status(self) -> None:
        if self._on_status:
            try:
                self._on_status()
            except Exception:
                _LOGGER.exception("Status callback failed")

    def _set_connected(self, value: bool) -> None:
        if value != self.connected:
            self.connected = value
            if self._on_connection:
                try:
                    self._on_connection(value)
                except Exception:
                    _LOGGER.exception("Connection callback failed")

    def backoff_delay(self, failures: int) -> float:
        # Cap the exponent: 2 ** 1024 overflows a float after ~42 days of
        # hourly retries, which used to crash the loop.
        exponent = min(max(failures - 1, 0), 30)
        delay = min(self._backoff_base * 2 ** exponent, self._backoff_max)
        return delay * random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER) if delay < self._backoff_max else delay

    def _handle(self, data: bytes) -> None:
        new_keys: set[str] = set()
        new_tables: set[str] = set()
        changed = False
        bursts = self.state.bursts
        for raw, msg in self._parser.feed(data):
            if self._on_raw:
                try:
                    self._on_raw(raw, msg)
                except Exception:  # never let logging break data flow
                    _LOGGER.exception("Raw data callback failed")
            if msg is None:
                continue
            try:
                had, keys, tables = self.state.apply(msg)
            except Exception:
                _LOGGER.exception("Could not process message: %r", raw[:200])
                continue
            changed |= had
            new_keys |= keys
            new_tables |= tables
        if changed and self._on_update:
            try:
                self._on_update(self.state, new_keys, new_tables)
            except Exception:  # a consumer bug must not kill the connection loop
                _LOGGER.exception("Update callback failed")
        if self.state.bursts != bursts and self._on_burst:
            try:
                self._on_burst(self.state)
            except Exception:
                _LOGGER.exception("Burst callback failed")

    async def _connect_and_read(self) -> bool:
        """One connection. Returns True if data flowed (healthy session)."""
        got_data = False
        if not self._routine:
            # After the module's routine 5-minute close we stay "connected";
            # otherwise the status (and HA history) would flap every 5 minutes.
            self._set_status(STATUS_CONNECTING)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self._connect_timeout
        )
        self._writer = writer
        try:
            self._parser = StreamParser()
            self._set_connected(True)
            while True:
                data = await asyncio.wait_for(reader.read(4096), self._idle_timeout)
                if not data:
                    if got_data:
                        # Routine: the module drops listen-only clients after ~5 min.
                        _LOGGER.debug("Device closed the connection; reconnecting")
                        return True
                    raise ConnectionError("closed by device before sending data")
                if not got_data:
                    got_data = True
                    if self.failures:
                        _LOGGER.warning(
                            "Reconnected to %s:%s after %d failed attempt(s)",
                            self.host, self.port, self.failures,
                        )
                    if self.status != STATUS_CONNECTED:
                        self.last_connected = time.time()
                    self.failures = 0
                    self.last_error = None
                    self.next_attempt = None
                    self._set_status(STATUS_CONNECTED)
                self._handle(data)
        finally:
            self._writer = None
            writer.close()
            self._set_connected(False)

    async def _maybe_rediscover(self) -> bool:
        """Scan the subnet for our device key if the module may have moved.

        Returns True if it was found at a new address (host updated).
        """
        if not self.device_key or not _is_private_ipv4(self.host):
            return False
        now = time.monotonic()
        due = self._auto_rediscover and self.failures >= self._rediscover_after and (
            self._last_scan is None or now - self._last_scan >= self._rediscover_interval
        )
        if not (due or self._scan_requested):
            return False
        self._scan_requested = False
        self._last_scan = now
        self._set_status(STATUS_SEARCHING)
        _LOGGER.info("Searching %s/24 for heat pump %s", self.host, self.device_key)
        try:
            found = await find_module(
                self.device_key, subnet_candidates(self.host), self.port,
                listen_timeout=self._scan_listen_timeout,
            )
        except Exception as err:  # scanning is best effort
            self.last_scan_result = f"scan failed: {_describe(err)}"
            _LOGGER.warning("Search for heat pump failed: %s", _describe(err))
            return False
        if found is None:
            self.last_scan_result = "not found"
            _LOGGER.info("Heat pump %s not found on the network", self.device_key)
            return False
        if found == self.host:
            self.last_scan_result = f"still at {found}"
            return False
        old, self.host = self.host, found
        self.last_scan_result = f"moved from {old} to {found}"
        _LOGGER.warning("Heat pump %s moved from %s to %s; reconnecting", self.device_key, old, found)
        if self._on_host_change:
            try:
                self._on_host_change(found, old)
            except Exception:
                _LOGGER.exception("Host change callback failed")
        return True

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self._connect_and_read()
                delay = self._clean_reconnect_delay
                self._routine = True
            except asyncio.CancelledError:
                raise
            except Exception as err:  # OSError, timeouts, and anything unexpected
                self._routine = False
                if self.status == STATUS_CONNECTED:
                    self.last_disconnected = time.time()
                self.failures += 1
                self.last_error = _describe(err)
                delay = self.backoff_delay(self.failures)
                if not isinstance(err, (OSError, asyncio.TimeoutError, ConnectionError)):
                    _LOGGER.exception("Unexpected error in connection to %s:%s", self.host, self.port)
                log = _LOGGER.warning if self.failures == 1 else _LOGGER.info
                log(
                    "Connection to %s:%s failed (%s); attempt %d, retrying in %s",
                    self.host, self.port, self.last_error, self.failures, _fmt_delay(delay),
                )
                if await self._maybe_rediscover():
                    delay = 0
            if self._wake.is_set():
                # reconnect_now() was pressed during the session: go again now
                delay = 0
                self._routine = False
            if not self._routine:
                self.next_attempt = time.time() + delay
                self._set_status(STATUS_WAITING)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            self.next_attempt = None
