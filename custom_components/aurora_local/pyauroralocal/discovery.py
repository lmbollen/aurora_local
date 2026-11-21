"""Finding the module: one-off identity probe and subnet search.

Both only open TCP connections and listen; nothing is ever sent.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

from .framing import StreamParser
from .state import DEVICE_KEY_RE, AdlarState, device_key_of

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0
LISTEN_TIMEOUT = 120.0  # the module names itself at least once per ~91 s burst
SCAN_CONNECT_TIMEOUT = 1.5
SCAN_PARALLEL = 64


async def _first_identity(reader: asyncio.StreamReader, timeout: float) -> tuple[str, Any] | None:
    """Read until a message names its device key. Returns (key, params), or
    None if the connection closes first. Raises TimeoutError after `timeout`."""
    parser = StreamParser()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        data = await asyncio.wait_for(reader.read(4096), remaining)
        if not data:
            return None
        for _raw, msg in parser.feed(data):
            params = (msg or {}).get("params")
            key = device_key_of(params)
            if key:
                return key, params


async def probe(host: str, port: int, timeout: float = LISTEN_TIMEOUT) -> AdlarState:
    """Connect once and wait for the first message that names the device.

    Raises ConnectionError if the host can't be reached, refuses, or closes
    the connection without naming itself, and TimeoutError if it connects
    but sends no device key within `timeout` seconds.
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
    except (TimeoutError, OSError) as err:
        raise ConnectionError(f"no connection to {host}:{port}: {err}") from err
    try:
        found = await _first_identity(reader, timeout)
    except OSError as err:
        raise ConnectionError(f"connection to {host}:{port} failed: {err}") from err
    finally:
        writer.close()
    if found is None:
        raise ConnectionError("connection closed by device")
    state = AdlarState()
    state.device_key, params = found
    product = params.get("productKey") if isinstance(params, dict) else None
    if isinstance(product, str) and DEVICE_KEY_RE.match(product):
        state.product_key = product
    return state


async def find_module(
    device_key: str,
    candidates: list[str],
    port: int = 8899,
    connect_timeout: float = SCAN_CONNECT_TIMEOUT,
    listen_timeout: float = LISTEN_TIMEOUT,
    parallel: int = SCAN_PARALLEL,
) -> str | None:
    """Find the host in `candidates` whose port `port` streams `device_key`.

    Step 1 checks which hosts accept a TCP connection (in parallel); step 2
    listens on each until a message names its device key. Sub-device keys
    such as "<key>-01" also match. Returns the host, or None.
    """
    sem = asyncio.Semaphore(parallel)

    async def is_open(host: str) -> str | None:
        async with sem:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(host, port), connect_timeout)
            except (TimeoutError, OSError):
                return None
            w.close()
            return host

    open_hosts = [h for h in await asyncio.gather(*(is_open(h) for h in candidates)) if h]
    _LOGGER.debug("Port %s open on %s", port, open_hosts)

    async def key_of(host: str) -> str | None:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), connect_timeout)
        except (TimeoutError, OSError):
            return None
        try:
            found = await _first_identity(reader, listen_timeout)
        except (TimeoutError, OSError):
            return None
        finally:
            writer.close()
        return found[0] if found else None

    pending = {asyncio.ensure_future(key_of(h)): h for h in open_hosts}
    try:
        while pending:
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                host = pending.pop(task)
                if not task.cancelled() and task.exception() is None and task.result() == device_key:
                    return host
    finally:
        for task in pending:
            task.cancel()
    return None


def subnet_candidates(host: str) -> list[str]:
    """All addresses in the /24 around `host` (IPv4 only; [] for a hostname)."""
    try:
        net = ipaddress.ip_network(f"{host}/24", strict=False)
    except ValueError:
        return []
    return [str(ip) for ip in net.hosts()]
