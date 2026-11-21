"""Split the module's byte stream into JSON messages."""

from __future__ import annotations

import codecs
import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

MAX_BUFFER = 64 * 1024
MAX_OBJECT = 16 * 1024  # a real message is < 1 KB
MESSAGE_START = '{"method"'


def _no_constant(name: str) -> None:
    """NaN / Infinity / -Infinity literals (not valid JSON) become None."""
    return None


class StreamParser:
    """Split a byte stream of back-to-back JSON objects.

    ``feed`` returns ``(raw_text, obj)`` pairs. ``obj`` is None for text that is
    not a JSON object (garbage between messages, malformed objects).
    """

    def __init__(self) -> None:
        self._buf = ""
        # Incremental decoder: a multi-byte UTF-8 character split across two
        # reads is joined instead of turned into replacement characters.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes) -> list[tuple[str, dict[str, Any] | None]]:
        self._buf += self._decoder.decode(data)
        out: list[tuple[str, dict[str, Any] | None]] = []
        while True:
            start = self._buf.find("{")
            if start < 0:
                # Hold non-JSON text until the next message starts, so a run
                # of junk becomes one raw record rather than many fragments.
                if len(self._buf) > MAX_BUFFER:
                    out.append((self._buf, None))
                    self._buf = ""
                break
            if start > 0:
                junk = self._buf[:start]
                if junk.strip():
                    out.append((junk, None))
                self._buf = self._buf[start:]
            end = self._find_object_end()
            # Resync: every real message starts with {"method"; another one
            # inside an unfinished object means this object is corrupt (e.g. a
            # lost "}" or a stray quote). Drop it as junk instead of waiting
            # until MAX_BUFFER, which would silence data for minutes.
            nxt = self._next_message_start()
            if nxt > 0 and (end is None or end > nxt):
                out.append((self._buf[:nxt], None))
                self._buf = self._buf[nxt:]
                continue
            if end is None:
                if len(self._buf) > MAX_OBJECT:
                    _LOGGER.warning("Discarding %d bytes of unterminated data", len(self._buf))
                    out.append((self._buf, None))
                    self._buf = ""
                break
            raw = self._buf[: end + 1]
            self._buf = self._buf[end + 1 :]
            try:
                obj = json.loads(raw, parse_constant=_no_constant)
            except (ValueError, RecursionError):
                _LOGGER.debug("Malformed message: %r", raw[:200])
                obj = None
            out.append((raw, obj if isinstance(obj, dict) else None))
        return out

    def _next_message_start(self) -> int:
        """Position of the next `{"method"` that starts a new message.

        One that follows ':', ',' or '[' is a nested value inside the current
        message, not a new one.
        """
        pos = self._buf.find(MESSAGE_START, 1)
        while pos > 0:
            before = self._buf[:pos].rstrip()
            if not before or before[-1] not in ":,[":
                return pos
            pos = self._buf.find(MESSAGE_START, pos + 1)
        return -1

    def _find_object_end(self) -> int | None:
        depth = 0
        in_str = False
        escape = False
        for i, ch in enumerate(self._buf):
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        return None
