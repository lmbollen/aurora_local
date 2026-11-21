"""Aurora Local: an Adlar Castra heat pump, read locally from its HF-LPT270 Wi-Fi module."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_MIGRATE_FROM,
    CONF_NOTIFY,
    CONF_SCAN,
    DOMAIN,
    HISTORY_VERSION,
    ISSUE_REBUILD,
)
from .coordinator import AdlarCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SELECT, Platform.NUMBER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
SERVICE_REBUILD = "rebuild_history"
REBUILD_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})

type AdlarConfigEntry = ConfigEntry[AdlarCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def rebuild(call: ServiceCall) -> ServiceResponse:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        entries = [e for e in hass.config_entries.async_loaded_entries(DOMAIN)
                   if entry_id is None or e.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entry")
        results = {}
        for entry in entries:
            try:
                results[entry.title] = await entry.runtime_data.async_rebuild_history()
            except HomeAssistantError:
                raise
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="rebuild_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_REBUILD}_{entry.entry_id}")
        return results

    hass.services.async_register(
        DOMAIN, SERVICE_REBUILD, rebuild, schema=REBUILD_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: AdlarConfigEntry) -> bool:
    if entry.version > 1:
        return False  # from a newer version: can't downgrade
    if entry.minor_version < 2:
        # 0.8.0 made notifications and the automatic subnet search opt-in.
        # Keep them on for entries created before that, as they were.
        options = {CONF_NOTIFY: True, CONF_SCAN: True, **entry.options}
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AdlarConfigEntry) -> bool:
    if CONF_MIGRATE_FROM in entry.data:
        from .legacy import async_migrate

        await async_migrate(hass, entry)
    coordinator = AdlarCoordinator(hass, entry, entry.data[CONF_HOST], entry.data[CONF_PORT])
    entry.runtime_data = coordinator
    # Load the remembered field list first so platforms can recreate
    # dynamic entities before the first data burst arrives.
    await coordinator.async_start()
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_stop()  # don't leave a connection running
        raise
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _offer_history_rebuild(hass, entry, coordinator)
    return True


def _offer_history_rebuild(hass: HomeAssistant, entry: AdlarConfigEntry, coordinator: AdlarCoordinator) -> None:
    """After an upgrade that changes how history is calculated, ask (as a
    repair) before rewriting statistics. Nothing to offer without an archive
    or on a fresh install (no history yet)."""
    issue_id = f"{ISSUE_REBUILD}_{entry.entry_id}"
    if coordinator.history_version >= HISTORY_VERSION or coordinator.raw_logger is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    if not coordinator.seen_keys:
        coordinator.history_version = HISTORY_VERSION  # fresh install
        coordinator.request_save(5)
        return
    ir.async_create_issue(
        hass, DOMAIN, issue_id, is_fixable=True, is_persistent=False,
        severity=ir.IssueSeverity.WARNING, translation_key=ISSUE_REBUILD,
        data={"entry_id": entry.entry_id},
    )


async def async_unload_entry(hass: HomeAssistant, entry: AdlarConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: AdlarConfigEntry) -> None:
    coordinator = entry.runtime_data
    # Rediscovery updates entry.data[host] while connected to the new address;
    # only reload for real changes (options, or a host set via Reconfigure).
    if (
        dict(entry.options) == coordinator.options_snapshot
        and entry.data[CONF_HOST] == coordinator.client.host
        and entry.data[CONF_PORT] == coordinator.client.port
    ):
        return
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_reload(entry.entry_id)
