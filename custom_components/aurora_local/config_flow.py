"""Config flow: ask for host/port and confirm the heat pump is talking."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_MIGRATE_FROM,
    CONF_NOTIFY,
    CONF_RAW_LOG,
    CONF_RETENTION_DAYS,
    CONF_SCAN,
    DEFAULT_NOTIFY,
    DEFAULT_PORT,
    DEFAULT_RAW_LOG,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SCAN,
    DOMAIN,
)
from .legacy import legacy_entries
from .pyauroralocal import AdlarState, probe

TITLE = "Adlar Castra heat pump"
SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
    }
)


async def _probe(user_input: dict[str, Any], errors: dict[str, str]) -> AdlarState | None:
    """Probe the module; on failure fill `errors` and return None."""
    try:
        return await probe(user_input[CONF_HOST], user_input[CONF_PORT])
    except TimeoutError:
        errors["base"] = "no_data"
    except OSError:  # includes ConnectionError
        errors["base"] = "cannot_connect"
    return None


class AdlarConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 2  # 2: notifications and automatic subnet search are opt-in

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AdlarOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None and legacy_entries(self.hass):
            return self.async_show_menu(step_id="user", menu_options=["migrate", "manual"])
        return await self.async_step_manual(user_input)

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and (state := await _probe(user_input, errors)):
            await self.async_set_unique_id(state.device_key)
            # Re-adding a known pump just updates its address (reload via the
            # entry's update listener).
            self._abort_if_unique_id_configured(updates=user_input, reload_on_update=False)
            return self.async_create_entry(title=TITLE, data=user_input)
        return self.async_show_form(
            step_id="manual",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_migrate(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take over an install from when this integration was called adlar_castra."""
        candidates = legacy_entries(self.hass)
        if not candidates:
            return self.async_abort(reason="nothing_to_migrate")
        old = candidates[0]
        if user_input is None:
            return self.async_show_form(step_id="migrate", description_placeholders={"title": old.title})
        await self.async_set_unique_id(old.unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=old.title,
            data={**old.data, CONF_MIGRATE_FROM: old.entry_id},
            options=dict(old.options),
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the module's address (e.g. after a DHCP change)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None and (state := await _probe(user_input, errors)):
            await self.async_set_unique_id(state.device_key)
            self._abort_if_unique_id_mismatch(reason="wrong_device")
            # The entry's update listener reloads it for the new address.
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, **user_input})
            return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input or dict(entry.data)),
            errors=errors,
        )


class AdlarOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_NOTIFY, default=options.get(CONF_NOTIFY, DEFAULT_NOTIFY)): bool,
                vol.Required(CONF_SCAN, default=options.get(CONF_SCAN, DEFAULT_SCAN)): bool,
                vol.Required(CONF_RAW_LOG, default=options.get(CONF_RAW_LOG, DEFAULT_RAW_LOG)): bool,
                vol.Required(
                    CONF_RETENTION_DAYS,
                    default=options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
                ): vol.All(int, vol.Range(min=1, max=3650)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
