"""Offline tests for AdlarClient reconnect behaviour against a fake module.

Usage: python test_reconnect.py <custom_components/aurora_local>
"""

import asyncio
import socket
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(sys.argv[1])

MSG = (b'{"method":"thing.property.post","id":"1","params":{"deviceKey":"AA",'
       b'"productKey":"P","temp_in":287},"version":"1.0","crc":1}')


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeModule:
    """mode: 'send' (send MSG then keep open), 'send_close' (send then close),
    'silent_close' (accept, close without data), 'silent' (accept, say nothing)."""

    def __init__(self, port: int, mode: str) -> None:
        self.port, self.mode, self.server, self.connections = port, mode, None, 0

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", self.port)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, reader, writer):
        self.connections += 1
        if self.mode in ("send", "send_close"):
            writer.write(MSG)
            await writer.drain()
        if self.mode in ("send_close", "silent_close"):
            writer.close()
            return
        try:
            await asyncio.sleep(30)
        finally:
            writer.close()


async def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def main():
    port = free_port()
    updates = []
    statuses = []

    def on_update(state, keys, tables):
        updates.append(dict(state.values))

    client = lib.client.AdlarClient(
        "127.0.0.1", port, on_update, on_status=lambda: statuses.append(client.status),
        backoff_base=0.05, backoff_max=0.4, connect_timeout=1, idle_timeout=0.5,
        clean_reconnect_delay=0.2,
    )

    # 1. Nothing listening: failures grow, delays double and cap at max.
    client.start()
    assert await wait_for(lambda: client.failures >= 6, 5), client.failures
    delays = [round(client.backoff_delay(n), 3) for n in range(1, 8)]
    assert 0.045 <= delays[0] <= 0.055 and 0.09 <= delays[1] <= 0.11 and 0.18 <= delays[2] <= 0.22
    assert delays[3] == 0.4 and delays[6] == 0.4, delays
    assert client.status in (lib.client.STATUS_WAITING, lib.client.STATUS_CONNECTING)
    assert "ConnectionRefusedError" in client.last_error or "OSError" in client.last_error, client.last_error
    print(f"1 backoff: OK  failures={client.failures} delays={delays} error={client.last_error!r}")

    # Real-world caps: 5 s doubling to 1 h
    real = lib.client.AdlarClient("x", 1, on_update)
    seq = [round(real.backoff_delay(n)) for n in (1, 2, 3, 4, 10, 11, 20)]
    assert 4 <= seq[0] <= 6 and seq[-1] == 3600 and seq[-2] == 3600, seq
    print(f"  real schedule (s): {seq}")

    # 2. Module comes back while we're in long backoff: reconnect_now connects at once.
    client._backoff_base = client._backoff_max = 30.0  # simulate a long wait
    assert await wait_for(lambda: client.status == lib.client.STATUS_WAITING and client.next_attempt
                          and client.next_attempt - time.time() > 10, 5)
    fake = FakeModule(port, "send")
    await fake.start()
    t = time.monotonic()
    client.reconnect_now()
    assert await wait_for(lambda: client.status == lib.client.STATUS_CONNECTED, 2), client.status
    took = time.monotonic() - t
    assert client.failures == 0 and client.last_error is None and updates
    print(f"2 reconnect_now during 30 s backoff: OK  connected in {took:.2f}s")

    # 3. Reconnect button while connected forces a fresh connection.
    before = fake.connections
    client.reconnect_now()
    assert await wait_for(lambda: fake.connections > before and client.status == lib.client.STATUS_CONNECTED, 2)
    print("3 reconnect_now while connected: OK  fresh connection made")
    await fake.stop()

    # 4. Routine close after data: quick reconnect, not counted as failure.
    client._backoff_base, client._backoff_max = 0.05, 0.4
    client.reconnect_now()
    fake = FakeModule(port, "send_close")
    await fake.start()
    assert await wait_for(lambda: client.status == lib.client.STATUS_CONNECTED, 2)
    seen = set()
    last_conn = client.last_connected
    end = time.monotonic() + 1.2
    while time.monotonic() < end:
        seen.add(client.status)
        await asyncio.sleep(0.005)
    assert fake.connections >= 3 and client.failures == 0, (fake.connections, client.failures)
    assert seen == {lib.client.STATUS_CONNECTED}, seen
    assert client.last_connected == last_conn, "last_connected must not change on routine reconnects"
    print(f"4 routine close: OK  {fake.connections} reconnects, failures 0, status stayed {seen}")
    await fake.stop()

    # 5. Module accepts but closes without data: counted as failure (no 1 s hammering).
    fake = FakeModule(port, "silent_close")
    await fake.start()
    client.reconnect_now()
    assert await wait_for(lambda: client.failures >= 2, 3), client.failures
    assert "before sending data" in (client.last_error or ""), client.last_error
    print(f"5 silent close: OK  failures={client.failures} error={client.last_error!r}")
    await fake.stop()

    # 6. Module accepts and stays silent: idle timeout counts as failure.
    fake = FakeModule(port, "silent")
    await fake.start()
    client.reconnect_now()
    assert await wait_for(lambda: client.failures >= 1 and "Timeout" in (client.last_error or ""), 3), client.last_error
    print(f"6 silent module: OK  error={client.last_error!r}")
    await fake.stop()

    # 7. A crashing update callback must not kill the loop.
    def boom(*a):
        raise RuntimeError("consumer bug")
    client._on_update = boom
    fake = FakeModule(port, "send_close")
    await fake.start()
    client.reconnect_now()
    await asyncio.sleep(0.8)
    assert fake.connections >= 2 and client._task and not client._task.done()
    print(f"7 crashing callback: OK  loop alive, {fake.connections} connections")
    await fake.stop()

    # 8. Loop task killed from outside: it restarts itself.
    client._on_update = on_update
    client._task.cancel()
    await asyncio.sleep(0.01)
    # A cancelled task is ignored by _task_done; report an unexpected end instead.
    client._task_done(types.SimpleNamespace(cancelled=lambda: False, exception=lambda: RuntimeError("x")))
    assert await wait_for(lambda: client._task is not None and not client._task.done(), 1)
    print("8 self-restart after unexpected end: OK")

    # 9. stop()
    await client.stop()
    assert client.status == lib.client.STATUS_STOPPED
    print("9 stop: OK  status=stopped")
    print(f"\nstatus callbacks fired: {len(statuses)}")


asyncio.run(main())
