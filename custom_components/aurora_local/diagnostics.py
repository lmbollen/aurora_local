"""Diagnostics download: everything the heat pump has reported, redacted.

People attach this file to public bug reports, so identifiers are removed:
the device key (also inside raw messages and the archive path), the product
key and the module's IP address.
"""

from __future__ import annotations

import re
import time
from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

from . import AdlarConfigEntry

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IDENTITY_SUFFIXES = ("deviceKey", "productKey")


def _scrub(text: str, secrets: list[str]) -> str:
    """Remove identifiers and every IPv4 address (old ones too, after a move)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return IPV4_RE.sub(REDACTED, text)


def _scrub_values(values: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    """Field values, without identity fields and with text scrubbed."""
    return {
        k: (_scrub(v, secrets) if isinstance(v, str) else v)
        for k, v in values.items()
        if not k.endswith(IDENTITY_SUFFIXES)
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AdlarConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    client = coordinator.client
    raw = coordinator.raw_logger
    cal = coordinator.calibration
    now = time.time()
    secrets = [str(s) for s in (entry.unique_id, client.state.product_key, client.host) if s]
    return {
        "integration": {
            "history_version": coordinator.history_version,
            "migrations": sorted(coordinator.migrations),
        },
        "connection": {
            "status": client.status,
            "connected": client.connected,
            "receiving_data": coordinator.fresh,
            "failed_attempts": client.failures,
            "last_error": _scrub(client.last_error or "", secrets) or None,
            "next_attempt": client.next_attempt,
            "last_connected": client.last_connected,
            "last_disconnected": client.last_disconnected,
            "last_search": _scrub(client.last_scan_result or "", secrets) or None,
            "data_age_seconds": client.state.age(),
        },
        "calibration": {
            "mode": cal.mode,
            "auto_offset": cal.auto_offset(now),
            "samples_7d": len(cal.recent(now)),
            "samples_total": len(cal.samples),
            "manual_inlet": cal.manual_inlet,
            "manual_outlet": cal.manual_outlet,
            "heat_produced_calibrated_kwh": round(coordinator.heat_energy.kwh, 3),
        },
        "low_flow": {"active": coordinator.low_flow.active(), "reason": coordinator.low_flow.reason},
        "message_methods": {_scrub(k, secrets): n for k, n in client.state.methods.items()},
        "values": _scrub_values(client.state.values, secrets),
        "tables": client.state.tables,
        "raw_log": {
            "enabled": raw is not None,
            "size_bytes": await hass.async_add_executor_job(raw.size_bytes) if raw else 0,
            "dropped_lines": raw.dropped if raw else 0,
        },
        "recent_raw_messages": [
            {**m, "raw": _scrub(m["raw"], secrets)} for m in coordinator.recent_raw
        ],
    }
