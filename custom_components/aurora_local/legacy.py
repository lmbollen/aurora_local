"""Take over an install from before 0.8.0, when this integration was `adlar_castra`.

Adding Aurora Local while an `adlar_castra` entry exists offers to migrate it
(see config_flow). The new entry then takes over the old one's device and
entities, so entity IDs, their history and long-term statistics, and any
customisation stay as they were. The stored calibration and the raw archive
move along, and the old entry is removed.

Uses `EntityRegistry.async_update_entity_platform`, Home Assistant's API for
moving entities between integrations; it needs the old entry unloaded, which
is done first. Can be deleted once no `adlar_castra` installs are left.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import CONF_MIGRATE_FROM, DOMAIN, LEGACY_DOMAIN, LEGACY_RAW_LOG_DIR, RAW_LOG_DIR, STORE_VERSION

_LOGGER = logging.getLogger(__name__)


def legacy_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """`adlar_castra` entries that no Aurora Local entry has taken over yet."""
    taken = {e.unique_id for e in hass.config_entries.async_entries(DOMAIN)}
    return [e for e in hass.config_entries.async_entries(LEGACY_DOMAIN) if e.unique_id not in taken]


def _move_raw_archive(src: str, dst: str) -> str:
    """Blocking. Rename the archive folder unless the new one already exists."""
    if not os.path.isdir(src):
        return "no legacy archive"
    if os.path.exists(dst):
        return f"kept {src}: {dst} already exists"
    os.replace(src, dst)
    return f"moved {src} -> {dst}"


async def async_migrate(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move everything from the legacy entry named in entry.data to `entry`."""
    old_id = entry.data[CONF_MIGRATE_FROM]
    old = hass.config_entries.async_get_entry(old_id)
    if old is not None:
        # Entities can only change integration while they aren't loaded.
        if not await hass.config_entries.async_unload(old_id):
            raise ConfigEntryNotReady("Could not unload the adlar_castra entry; restart and try again")

        # Entities first: once the device leaves the old entry, Home
        # Assistant deletes that entry's entities on it.
        ent_reg = er.async_get(hass)
        moved = 0
        for ent in er.async_entries_for_config_entry(ent_reg, old_id):
            ent_reg.async_update_entity_platform(
                ent.entity_id, DOMAIN, new_config_entry_id=entry.entry_id
            )
            moved += 1
        dev_reg = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(dev_reg, old_id):
            dev_reg.async_update_device(
                device.id,
                new_config_entry_id=entry.entry_id,
                new_identifiers={(DOMAIN, ident) for dom, ident in device.identifiers
                                 if dom == LEGACY_DOMAIN},
            )

        old_store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{LEGACY_DOMAIN}.{old_id}")
        new_store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        data = await old_store.async_load()
        if data is not None and await new_store.async_load() is None:
            await new_store.async_save(data)
        result = await hass.async_add_executor_job(
            _move_raw_archive, hass.config.path(LEGACY_RAW_LOG_DIR), hass.config.path(RAW_LOG_DIR)
        )
        # Everything is moved, so removing the old entry deletes nothing else.
        await hass.config_entries.async_remove(old_id)
        await old_store.async_remove()
        _LOGGER.info(
            "Migrated %s entry %s to %s: %d entities, stored data %s, raw archive: %s",
            LEGACY_DOMAIN, old.title, DOMAIN, moved, "copied" if data else "none", result,
        )
    data = {k: v for k, v in entry.data.items() if k != CONF_MIGRATE_FROM}
    hass.config_entries.async_update_entry(entry, data=data)
