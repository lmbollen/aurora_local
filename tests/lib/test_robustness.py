"""Library behaviour found in the pre-release reviews (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
import gzip
import json
import socket
from pathlib import Path

import pytest
from pyauroralocal import AdlarClient, AdlarState, BurstProcessor, FaultTracker, RawLogger, StreamParser
from pyauroralocal import fields as F
from pyauroralocal.client import _is_private_ipv4

KEY = "AABBCCDDEEFF"


def msg(params: dict) -> bytes:
    return json.dumps({"method": "thing.property.post", "params": {"deviceKey": KEY, **params}}).encode()


# -- parsing ---------------------------------------------------------------

def test_nested_method_key_is_not_split() -> None:
    raw = b'{"method":"thing.event.post","params":{"x":{"method":1}}}'
    out = StreamParser().feed(raw)
    assert len(out) == 1 and out[0][1] is not None and out[0][1]["params"]["x"] == {"method": 1}


def test_resync_after_missing_brace_still_works() -> None:
    raw = b'{"method":"a","params":{"t":1},"crc":1{"method":"b","params":{"t":2}}'
    objs = [o for _, o in StreamParser().feed(raw) if o]
    assert [o["method"] for o in objs] == ["b"]


@pytest.mark.parametrize("literal", [b"1e999", b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_numbers_become_unknown(literal: bytes) -> None:
    raw = b'{"method":"m","params":{"param1":' + literal + b',"instantpower":500,"device_fault1":' + literal + b"}}"
    state = AdlarState()
    for _, obj in StreamParser().feed(raw):
        state.apply(obj)
    assert F.compressor_hz(state.values) is None
    FaultTracker().update(state.values)  # must not raise
    BurstProcessor().process(state.values, 1000.0, 1000.0)  # must not raise


def test_product_key_is_validated_and_methods_are_capped() -> None:
    state = AdlarState()
    state.apply({"method": "m", "params": {"productKey": 12345}})
    assert state.product_key is None
    for i in range(500):
        state.apply({"method": f"m{i}" * 50, "params": {}})
    assert len(state.methods) <= 50 and all(len(m) <= 64 for m in state.methods)


def test_deep_nesting_is_ignored() -> None:
    params: dict = {"a": 1}
    for _ in range(200):
        params = {"n": params}
    state = AdlarState()
    state.apply({"method": "m", "params": params})  # no RecursionError
    assert state.values == {}


# -- faults ------------------------------------------------------------------

def test_fault_memory_survives_a_run_without_data() -> None:
    tracker = FaultTracker(["P01"])
    assert tracker.last_known == ["P01"]  # nothing seen this run: keep what was restored
    change = tracker.update({"device_fault7": 0})
    assert change is not None and change.cleared == ["P01"]
    assert tracker.last_known == []


# -- archive -------------------------------------------------------------------

def _line(raw: bytes, ts: str = "2026-01-01T00:00:00.000+00:00") -> str:
    return json.dumps({"ts": ts, "raw": raw.decode(), "ok": True})


def test_replay_skips_bad_lines_and_counts_bursts_once(tmp_path: Path) -> None:
    day = tmp_path / "2026-01-01.jsonl"
    day.write_text("\n".join([
        _line(msg({"param1": 0})),
        _line(b'{"method":"m","params":{"param1":1e999,"instantpower":1}}'),
        "not json",
        _line(msg({"instantpower": 400})),
    ]) + "\n", encoding="utf-8")
    bursts = []
    RawLogger(tmp_path, 30).replay(None, lambda v, ts: bursts.append(ts))
    assert len(bursts) == 2


def test_housekeeping_only_touches_day_files(tmp_path: Path) -> None:
    (tmp_path / "2000-01-01.jsonl").write_text("x\n")
    (tmp_path / "2000-01-02.jsonl.gz").write_bytes(gzip.compress(b"x\n"))
    (tmp_path / "0-notes.txt").write_text("keep me")
    RawLogger(tmp_path, 30).flush([])  # old days are past retention
    assert [p.name for p in tmp_path.iterdir()] == ["0-notes.txt"]


def test_pending_buffer_is_bounded_in_bytes(tmp_path: Path) -> None:
    logger = RawLogger(tmp_path, 30)
    big = "x" * 100_000
    for _ in range(3000):
        logger.add(big, False)
    assert logger._pending_bytes <= 32 * 1024 * 1024 and logger.dropped > 0


# -- client ------------------------------------------------------------------

def test_only_private_ipv4_is_scanned() -> None:
    assert _is_private_ipv4("192.168.1.10") and _is_private_ipv4("10.0.0.1") and _is_private_ipv4("127.0.0.5")
    assert not _is_private_ipv4("8.8.8.8") and not _is_private_ipv4("heatpump.local")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_on_burst_fires_once_per_burst_and_scan_is_opt_in() -> None:
    port = _free_port()
    data = msg({"temp_in": 300}) + msg({"param1": 30}) + msg({"instantpower": 500})

    async def handler(reader, writer):
        writer.write(data)
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.write(msg({"instantpower": 510}))
        await writer.drain()
        await asyncio.sleep(5)
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", port)
    bursts, updates = [], []
    client = AdlarClient("127.0.0.1", port, lambda s, k, t: updates.append(len(k)),
                         on_burst=lambda s: bursts.append(s.bursts), device_key=KEY)
    client.start()
    for _ in range(100):
        if len(bursts) >= 2:
            break
        await asyncio.sleep(0.02)
    await client.stop()
    server.close()
    await server.wait_closed()
    assert bursts == [1, 2] and updates
    # without auto_rediscover, repeated failures never trigger a scan
    client2 = AdlarClient("127.0.0.1", 9, device_key=KEY)
    client2.failures = 10
    assert await client2._maybe_rediscover() is False and client2._last_scan is None
