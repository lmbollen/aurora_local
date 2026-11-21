"""Offline test: the client finds a moved module by device key.

Usage: python test_rediscover.py <custom_components/aurora_local>
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load  # noqa: E402

lib = load(sys.argv[1])

PORT = 18899


def msg(key: str) -> bytes:
    return (f'{{"method":"thing.sub.logout","id":"1","params":{{"productKey":"P","deviceKey":"{key}-01",'
            f'"bandUser":2}},"version":"1.0","crc":1}}'
            f'{{"method":"thing.property.post","id":"2","params":{{"deviceKey":"{key}","temp_in":287}},'
            f'"version":"1.0","crc":1}}').encode()


async def serve(host: str, key: str):
    async def handler(reader, writer):
        await asyncio.sleep(0.2)
        writer.write(msg(key))
        await writer.drain()
        try:
            await asyncio.sleep(30)
        finally:
            writer.close()
    return await asyncio.start_server(handler, host, PORT)


async def wait_for(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def main():
    decoy = await serve("127.0.0.9", "BBBBBBBBBBBB")
    real = await serve("127.0.0.7", "AABBCCDDEEFF")

    # find_module directly
    t = time.monotonic()
    found = await lib.discovery.find_module("AABBCCDDEEFF", lib.discovery.subnet_candidates("127.0.0.5"), PORT,
                                connect_timeout=0.3, listen_timeout=3)
    assert found == "127.0.0.7", found
    print(f"1 find_module: OK  found {found} in {time.monotonic() - t:.1f}s (decoy at 127.0.0.9 ignored)")
    assert await lib.discovery.find_module("NOPE", lib.discovery.subnet_candidates("127.0.0.5"), PORT,
                               connect_timeout=0.3, listen_timeout=1) is None
    print("2 unknown key: OK  None")

    # client: configured at a dead address, rediscovers after 2 failures
    moves = []
    client = lib.client.AdlarClient(
        "127.0.0.5", PORT, lambda *a: None, backoff_base=0.05, backoff_max=0.2, connect_timeout=0.3,
        idle_timeout=5, device_key="AABBCCDDEEFF", on_host_change=lambda new, old: moves.append((old, new)),
        rediscover_after=2, rediscover_interval=600, scan_listen_timeout=3, auto_rediscover=True,
    )
    client.start()
    ok = await wait_for(lambda: client.status == lib.client.STATUS_CONNECTED, 15)
    assert ok, (client.status, client.host, client.last_scan_result)
    assert client.host == "127.0.0.7" and moves == [("127.0.0.5", "127.0.0.7")], (client.host, moves)
    print(f"3 client rediscovery: OK  {client.last_scan_result}, connected, callback {moves}")

    # interval respected: after more failures no second automatic scan within 10 min
    real.close(); await real.wait_closed()
    scans_before = client._last_scan
    await wait_for(lambda: client.failures >= 4, 5)
    assert client._last_scan == scans_before, "rescanned too early"
    print(f"4 scan interval: OK  {client.failures} failures, no extra scan")

    # reconnect button requests a scan on the next failure even within the interval
    real = await serve("127.0.0.11", "AABBCCDDEEFF")
    client.reconnect_now()
    ok = await wait_for(lambda: client.status == lib.client.STATUS_CONNECTED and client.host == "127.0.0.11", 15)
    assert ok, (client.status, client.host, client.last_scan_result)
    print(f"5 reconnect button triggers scan: OK  {client.last_scan_result}")

    await client.stop()
    for s in (decoy, real):
        s.close()
        await s.wait_closed()


asyncio.run(main())
