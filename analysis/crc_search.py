"""Find which byte range and function produce the `crc` field.

Usage: python crc_search.py <archive_dir>
Tries byte sums / CRC16 variants over candidate substrings of the raw message.
"""

import gzip
import json
import re
import sys
from pathlib import Path


def raws(d: Path, limit=4000):
    n = 0
    for f in sorted(d.iterdir()):
        op = gzip.open if f.suffix == ".gz" else open
        with op(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("ok"):
                    yield r["raw"]
                    n += 1
                    if n >= limit:
                        return


def crc16(data: bytes, poly: int, init: int, refin: bool) -> int:
    crc = init
    for b in data:
        if refin:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
        else:
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def candidates(raw: str):
    """Substrings of the raw message the crc might cover."""
    m = re.match(r'^(\{.*)(,"crc":(\d+)\})$', raw)
    if not m:
        return None, {}
    body, crc = m.group(1), int(m.group(3))
    pi = raw.find('"params":')
    params_start = pi + len('"params":')
    depth = 0
    for i in range(params_start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                params_end = i + 1
                break
    params = raw[params_start:params_end]
    return crc, {
        "params_obj": params,
        "params_inner": params[1:-1],
        "body_without_crc": body,
        "body_plus_brace": body + "}",
        "from_params_to_end": raw[pi:m.start(2)],
        "after_method": raw[raw.find(',"id"'):m.start(2)] if ',"id"' in raw else "",
    }


funcs = {
    "sum": lambda b: sum(b),
    "sum16": lambda b: sum(b) & 0xFFFF,
    "xor": lambda b: __import__("functools").reduce(lambda a, x: a ^ x, b, 0),
    "crc16_modbus": lambda b: crc16(b, 0xA001, 0xFFFF, True),
    "crc16_arc": lambda b: crc16(b, 0xA001, 0x0000, True),
    "crc16_ccitt_ffff": lambda b: crc16(b, 0x1021, 0xFFFF, False),
    "crc16_xmodem": lambda b: crc16(b, 0x1021, 0x0000, False),
}

data = []
for raw in raws(Path(sys.argv[1])):
    crc, cands = candidates(raw)
    if crc is not None:
        data.append((raw, crc, cands))
print(f"messages with crc: {len(data)}")

results = {}
for raw, crc, cands in data:
    for cname, text in cands.items():
        b = text.encode()
        for fname, fn in funcs.items():
            key = (cname, fname)
            hit = fn(b) == crc
            ok, tot = results.get(key, (0, 0))
            results[key] = (ok + hit, tot + 1)

best = sorted(results.items(), key=lambda kv: -kv[1][0])[:10]
for (c, f), (ok, tot) in best:
    print(f"{c:20s} {f:18s} {ok}/{tot}")

# show a few misses for the best combo
(c, f), (ok, tot) = best[0]
if ok < tot:
    print("\nexamples where best combo fails:")
    shown = 0
    for raw, crc, cands in data:
        v = funcs[f](cands[c].encode())
        if v != crc:
            print(f"  crc={crc} computed={v} diff={crc - v}: {raw[:160]}")
            shown += 1
            if shown >= 8:
                break
