"""Config flow, setup/unload and the adlar_castra takeover, inside Home Assistant."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aurora_local.const import CONF_MIGRATE_FROM, DOMAIN, LEGACY_DOMAIN
from custom_components.aurora_local.pyauroralocal import AdlarState

from .conftest import DEVICE_KEY

DATA = {"host": "192.0.2.10", "port": 8899}
OPTIONS = {"raw_log": False, "notify": False}


def _state(key: str = DEVICE_KEY) -> AdlarState:
    state = AdlarState()
    state.device_key = key
    return state


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    with patch("custom_components.aurora_local.config_flow.probe", return_value=_state()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == DEVICE_KEY
    await hass.async_block_till_done()


async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.aurora_local.config_flow.probe", side_effect=ConnectionError):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_setup_and_unload(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_KEY, data=DATA, options=OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "rebuild_history")
    # No data yet: measurement entities exist but are unavailable
    states = hass.states.async_all("sensor")
    temps = [s for s in states if s.attributes.get("device_class") == "temperature"]
    assert temps and all(s.state == "unavailable" for s in temps)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_takeover_of_adlar_castra_entry(hass: HomeAssistant, hass_storage) -> None:
    """Device, entities (with IDs and customisations) and stored data move over."""
    old = MockConfigEntry(domain=LEGACY_DOMAIN, unique_id=DEVICE_KEY, title="Heat pump",
                          data=DATA, options=OPTIONS)
    old.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=old.entry_id, identifiers={(LEGACY_DOMAIN, DEVICE_KEY)}, name="Heat pump"
    )
    dev_reg.async_update_device(device.id, name_by_user="Aurora III")
    ent = ent_reg.async_get_or_create(
        "sensor", LEGACY_DOMAIN, f"{DEVICE_KEY}_outdoor_temperature", config_entry=old,
        device_id=device.id, suggested_object_id="garden_outdoor",
    )
    ent_reg.async_update_entity(ent.entity_id, name="My outdoor sensor")
    hass_storage[f"{LEGACY_DOMAIN}.{old.entry_id}"] = {
        "version": 1, "minor_version": 1, "key": f"{LEGACY_DOMAIN}.{old.entry_id}",
        "data": {"history_version": 99, "migrations": ["disable_unknown_fields"],
                 "heat_calibrated_kwh": 123.4,
                 "calibration": {"mode": "manual", "manual_inlet": 0.0, "manual_outlet": -0.86,
                                 "samples": []}},
    }

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "migrate"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "migrate"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    new = result["result"]

    assert new.state is ConfigEntryState.LOADED
    assert CONF_MIGRATE_FROM not in new.data and new.data == DATA and dict(new.options) == OPTIONS
    assert hass.config_entries.async_get_entry(old.entry_id) is None

    moved = ent_reg.async_get(ent.entity_id)
    assert moved is not None, (
        "entity was deleted instead of moved",
        [(e.entity_id, e.platform, e.unique_id) for e in ent_reg.entities.values() if "garden" in e.entity_id or "outdoor" in e.unique_id],
        [(k, v.entity_id) for k, v in ent_reg.deleted_entities.items()],
    )
    assert moved.id == ent.id and moved.platform == DOMAIN and moved.config_entry_id == new.entry_id
    assert moved.name == "My outdoor sensor"

    dev = dev_reg.async_get(device.id)
    assert dev is not None and dev.config_entry_id == new.entry_id
    assert dev.identifiers == {(DOMAIN, DEVICE_KEY)} and dev.name_by_user == "Aurora III"

    coordinator = new.runtime_data
    assert coordinator.history_version == 99  # stored data came along
    assert coordinator.calibration.manual_outlet == -0.86
    assert round(coordinator.heat_energy.kwh, 1) == 123.4
    assert f"{LEGACY_DOMAIN}.{old.entry_id}" not in hass_storage

    # Adding the integration again is a normal setup, not another takeover
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "manual"
