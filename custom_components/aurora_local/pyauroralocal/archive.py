"""Raw data archive: every received message, exactly as received.

One JSON line per message in ``<dir>/YYYY-MM-DD.jsonl`` (UTC dates). ``raw``
is the exact text received; ``ok`` is false for text that was not a valid
JSON object. Finished days are gzipped, and files older than the retention
period are deleted. ``flush`` and ``replay`` do blocking file I/O and must
be run in an executor."""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import threading
import zlib
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .state import AdlarState

_LOGGER = logging.getLogger(__name__)

MAX_PENDING_BYTES = 32 * 1024 * 1024  # ~ a day of data; protects memory if the disk fails
MAX_RECORD = 16 * 1024                # longer junk is truncated (real messages are < 1 KB)
DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?(\.tmp)?$")

BurstCallback = Callable[[dict[str, Any], float], Any]


class RawLogger:
    def __init__(self, directory: Path, retention_days: int) -> None:
        self.directory = directory
        self.retention_days = retention_days
        self._pending: deque[str] = deque()
        self._pending_bytes = 0
        self._last_housekeeping: str | None = None
        self._lock = threading.Lock()  # flush, housekeeping and replay's file listing
        self.dropped = 0

    def add(self, raw: str, ok: bool) -> None:
        line = json.dumps(
            {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "raw": raw[:MAX_RECORD], "ok": ok},
            ensure_ascii=False,
        )
        while self._pending and self._pending_bytes + len(line) > MAX_PENDING_BYTES:
            self._pending_bytes -= len(self._pending.popleft())
            self.dropped += 1
        self._pending.append(line)
        self._pending_bytes += len(line)

    def take(self) -> list[str]:
        """Hand the pending lines to a flush (call from the event loop)."""
        lines, self._pending, self._pending_bytes = list(self._pending), deque(), 0
        return lines

    def flush(self, lines: list[str]) -> None:
        """Blocking: append lines to today's file and do housekeeping."""
        with self._lock:
            self._flush(lines)

    def _flush(self, lines: list[str]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if lines:
            with open(self.directory / f"{today}.jsonl", "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        if self._last_housekeeping != today:
            self._housekeeping(today)
            self._last_housekeeping = today

    def _housekeeping(self, today: str) -> None:
        """Gzip finished days, delete days past retention. Only day files are touched."""
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        for path in sorted(self.directory.iterdir()):
            match = DAY_FILE_RE.match(path.name)
            if not match:
                continue
            day = match.group(1)
            try:
                if path.name.endswith(".tmp"):
                    path.unlink()  # left over from an interrupted compression
                elif day < cutoff:
                    path.unlink()
                elif path.suffix == ".jsonl" and day < today:
                    # Write to a temporary name and rename, so a crash never
                    # leaves a half-written .gz next to the original.
                    tmp = Path(f"{path}.gz.tmp")
                    with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(tmp, f"{path}.gz")
                    path.unlink()
            except OSError as err:
                _LOGGER.warning("Raw log housekeeping failed for %s: %s", path, err)

    def _day_files(self, max_files: int | None) -> list[Path]:
        by_day: dict[str, Path] = {}
        with self._lock:  # not while housekeeping is compressing a day
            for p in self.directory.iterdir():
                match = DAY_FILE_RE.match(p.name)
                if not match or match.group(3):
                    continue
                day = match.group(1)
                # If a crash left both a day's .jsonl and its .jsonl.gz, read
                # only the compressed copy so the day isn't counted twice.
                if day not in by_day or p.suffix == ".gz":
                    by_day[day] = p
        files = [by_day[d] for d in sorted(by_day)]
        return files if max_files is None else files[-max_files:]

    def replay(self, max_files: int | None = 2, on_burst: BurstCallback | None = None,
               state: AdlarState | None = None) -> dict[str, Any]:
        """Blocking: replay the newest archive files through an AdlarState.

        Returns the latest value of every field (used to restore settings
        after a restart). `on_burst(values, epoch_seconds)` is called once per
        completed measurement burst (used for calibration, energy and history).
        """
        if state is None:
            state = AdlarState()
        if not self.directory.exists():
            return state.values
        for path in self._day_files(max_files):
            if not path.exists() and path.suffix == ".jsonl":
                path = Path(f"{path}.gz")  # compressed since it was listed
            opener = gzip.open if path.suffix == ".gz" else open
            try:
                with opener(path, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        self._replay_line(line, state, on_burst)
            except (OSError, EOFError, zlib.error) as err:
                # A truncated or corrupt file must not abort the whole replay.
                _LOGGER.warning("Could not fully read %s: %s", path, err)
        return state.values

    @staticmethod
    def _replay_line(line: str, state: AdlarState, on_burst: BurstCallback | None) -> None:
        try:
            rec = json.loads(line)
            if not rec.get("ok"):
                return
            msg = json.loads(rec["raw"], parse_constant=lambda _: None)
            before = state.bursts
            state.apply(msg)
            if on_burst and state.bursts != before:
                on_burst(state.values, datetime.fromisoformat(rec["ts"]).timestamp())
        except Exception as err:  # one bad line must never stop a replay
            _LOGGER.debug("Skipping archive line: %s", err)

    def size_bytes(self) -> int:
        """Blocking: total size of the archive."""
        if not self.directory.exists():
            return 0
        return sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
