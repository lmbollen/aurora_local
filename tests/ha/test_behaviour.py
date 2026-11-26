"""Entities after a burst, options, reconfigure, entry migration, repairs, diagnostics."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aurora_local.const import CONF_NOTIFY, CONF_SCAN, DOMAIN, ISSUE_REBUILD
from custom_components.aurora_local.diagnostics import async_get_config_entry_diagnostics
from custom_components.aurora_local.pyauroralocal import AdlarState

from .conftest import DEVICE_KEY

DATA = {"host": "192.0.2.10", "port": 8899}
OPTIONS = {"raw_log": False}

# One running burst: inlet 30.0, outlet 35.0 (5 K), 4000 W heat, 1000 W power
BURST = {
    "temp_in": 300, "temp_out": 350, "energy2": 4000, "instantpower": 1000, "param1": 50,
    "instantrate": 700, "temp_envir2": 65531, "device_fault7": 0, "functionadd": 1,
}


async def _setup(hass: HomeAssistant, **kw) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_KEY, data=DATA,
                            options=kw.pop("options", OPTIONS), minor_version=2, **kw)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _feed(entry: MockConfigEntry, values: dict) -> None:
    c = entry.runtime_data
    c.client.state.apply({"method": "thing.property.post", "params": {"deviceKey": DEVICE_KEY, **values}})
    c._handle_update(c.client.state, set(values), set())
    c._handle_burst(c.client.state)


def _state(hass: HomeAssistant, unique: str):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DEVICE_KEY}_{unique}")
    return hass.states.get(entity_id)


async def test_burst_updates_corrected_and_reported_sensors(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    coordinator.calibration.mode = "manual"
    coordinator.calibration.manual_outlet = -1.0
    _feed(entry, BURST)
    await hass.async_block_till_done()
    assert float(_state(hass, "temp_out").state) == 34.0       # corrected
    assert float(_state(hass, "temp_out_reported").state) == 35.0
    assert float(_state(hass, "delta_t").state) == 4.0
    assert _state(hass, "delta_t").attributes["device_class"] == "temperature_delta"
    assert float(_state(hass, "heat_output").state) == 3200    # 4000 * 4/5
    assert float(_state(hass, "cop").state) == 3.2
    assert float(_state(hass, "outdoor_temperature").state) == -0.5
    assert _state(hass, "quiet_mode").state == "on"
    assert _state(hass, "fault_code").state == "none"


async def test_refrigerant_sensors_and_generic_cleanup(hass: HomeAssistant) -> None:
    # An older version created a generic (disabled) entity for param60
    registry = er.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_KEY, data=DATA, options=OPTIONS, minor_version=2)
    entry.add_to_hass(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, f"{DEVICE_KEY}_param60", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    _feed(entry, {**BURST, "param60": 1052, "param58": 341, "param33": 65531, "param21": 344,
                  "param2": 65, "param25": 400, "param32": 70, "param5": 452, "param6": 20,
                  "param7": 65533, "param8": 281, "param16": 57, "param38": 8, "param10": 0, "param18": 0})
    await hass.async_block_till_done()
    assert float(_state(hass, "discharge_temperature").state) == 45.2
    assert float(_state(hass, "coil_temperature").state) == -0.3
    assert float(_state(hass, "compressor_current").state) == 5.7
    assert float(_state(hass, "suction_temperature").state) == 2.0
    assert float(_state(hass, "liquid_line_temperature").state) == 28.1
    # A field that is not covered keeps its (disabled) generic entity
    _feed(entry, {"param59": 1})
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{DEVICE_KEY}_param59") is not None
    flow = _state(hass, "water_flow_controller")
    assert float(flow.state) == 0.8 and flow.attributes["unit_of_measurement"] == "m³/h"
    defrost_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{DEVICE_KEY}_defrost")
    oil_id = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{DEVICE_KEY}_oil_return")
    assert hass.states.get(defrost_id).state == "on"   # 50 Hz with the four-way valve at 0
    assert hass.states.get(oil_id).state == "off"
    _feed(entry, {"param18": 1})
    await hass.async_block_till_done()
    assert hass.states.get(oil_id).state == "on"
    high = _state(hass, "high_pressure")
    assert float(high.state) == 10.52 and high.attributes["unit_of_measurement"] == "bar"
    assert float(_state(hass, "low_pressure").state) == 3.41
    assert float(_state(hass, "evaporating_temperature").state) == -0.5   # signed 16-bit
    assert float(_state(hass, "dc_bus_voltage").state) == 344
    assert float(_state(hass, "expansion_valve").state) == 65
    assert float(_state(hass, "outdoor_fan_speed").state) == 400
    assert float(_state(hass, "circulation_pump_speed").state) == 70
    # No generic entity is created for a field that has a named sensor
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{DEVICE_KEY}_param60") is None


async def test_fault_code_unknown_before_fault_data(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    _feed(entry, {k: v for k, v in BURST.items() if k != "device_fault7"})
    await hass.async_block_till_done()
    assert _state(hass, "fault_code").state == "unknown"


async def test_options_flow_has_scan_toggle_and_reloads(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert entry.runtime_data.client._auto_rediscover is False  # opt-in
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NOTIFY: False, CONF_SCAN: True, "raw_log": False, "retention_days": 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.client._auto_rediscover is True


async def test_reconfigure(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    other = AdlarState()
    other.device_key = "112233445566"
    result = await entry.start_reconfigure_flow(hass)
    with patch("custom_components.aurora_local.config_flow.probe", AsyncMock(return_value=other)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.0.2.20", "port": 8899})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_device"

    same = AdlarState()
    same.device_key = DEVICE_KEY
    result = await entry.start_reconfigure_flow(hass)
    with patch("custom_components.aurora_local.config_flow.probe", AsyncMock(return_value=same)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.0.2.20", "port": 8899})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["host"] == "192.0.2.20"
    assert entry.runtime_data.client.host == "192.0.2.20"  # reloaded once by the update listener


async def test_no_data_error(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch("custom_components.aurora_local.config_flow.probe", AsyncMock(side_effect=TimeoutError)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "no_data"}


async def test_old_entries_keep_notifications_and_scan(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_KEY, data=DATA, options=OPTIONS, minor_version=1)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert entry.options[CONF_NOTIFY] is True and entry.options[CONF_SCAN] is True


async def test_history_rebuild_is_offered_as_repair(hass: HomeAssistant, hass_storage) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_KEY, data=DATA,
                            options={"raw_log": True}, minor_version=2)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1, "minor_version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"keys": ["temp_in"], "history_version": 1},
    }
    entry.add_to_hass(hass)
    with patch("custom_components.aurora_local.coordinator.AdlarCoordinator.async_rebuild_history",
               AsyncMock(return_value={})) as rebuild:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        issue_id = f"{ISSUE_REBUILD}_{entry.entry_id}"
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
        rebuild.assert_not_called()  # nothing is rewritten without confirmation

        from custom_components.aurora_local.repairs import async_create_fix_flow

        flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
        flow.hass = hass
        result = await flow.async_step_confirm({})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        rebuild.assert_called_once()


async def test_diagnostics_are_redacted(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    c = entry.runtime_data
    _feed(entry, {**BURST, "productKey": "PRODKEY1", "note": "from 192.0.2.99", "x.deviceKey": DEVICE_KEY})
    c.client.last_scan_result = "moved from 192.0.2.5 to 192.0.2.10"
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in (DEVICE_KEY, "PRODKEY1", "192.0.2.99", "192.0.2.5", "192.0.2.10"):
        assert secret not in text, secret


async def test_defrost_heat_and_cop_today(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    c = entry.runtime_data
    # Defrost: four-way valve at 0 at 55 Hz, water cooled by 4.5 K; the pump reports +690 W
    _feed(entry, {"temp_in": 300, "temp_out": 255, "energy2": 690, "instantpower": 700, "param1": 55,
                  "param10": 0, "param38": 10, "power_use": 10000})
    await hass.async_block_till_done()
    assert float(_state(hass, "heat_output").state) == -5985
    assert float(_state(hass, "cop").state) == -8.55
    assert _state(hass, "heat_produced_calibrated").attributes["state_class"] == "total"
    c.daily.heat_kwh, c.daily.elec_kwh = 30.0, 10.0
    c.daily.previous = ("2026-11-24", 28.0, 10.0)
    c.async_update_listeners()
    await hass.async_block_till_done()
    today = _state(hass, "cop_today")
    assert float(today.state) == 3.0
    assert today.attributes["yesterday"] == 2.8 and today.attributes["electricity_kwh"] == 10.0
    assert today.attributes["state_class"] == "measurement"



async def test_finished_day_cop_written_to_statistics(hass: HomeAssistant) -> None:
    """Every hour of the finished local day gets the day's COP (official import API)."""
    from homeassistant.util import dt as dt_util

    from custom_components.aurora_local import history_import

    await hass.config.async_set_time_zone("Europe/Amsterdam")
    entry = await _setup(hass)
    with (
        patch.object(history_import, "get_instance") as instance,
        patch.object(history_import, "async_import_statistics") as imp,
    ):
        instance.return_value.async_add_executor_job = AsyncMock(return_value={})
        assert await history_import.async_write_daily_cop(entry.runtime_data, "2026-10-25", 3.25) == 25  # DST day
        assert await history_import.async_write_daily_cop(entry.runtime_data, "2026-11-24", 3.25) == 24
    meta, rows = imp.call_args.args[1], imp.call_args.args[2]
    assert meta["statistic_id"] == _state(hass, "cop_today").entity_id and meta["source"] == "recorder"
    assert dt_util.as_local(rows[0]["start"]).isoformat() == "2026-11-24T00:00:00+01:00"
    assert dt_util.as_local(rows[-1]["start"]).isoformat() == "2026-11-24T23:00:00+01:00"
    assert all(r["mean"] == r["min"] == r["max"] == 3.25 for r in rows)


async def test_instantrate_is_a_cop_and_old_flow_entity_is_removed(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_KEY, data=DATA, options=OPTIONS, minor_version=2)
    entry.add_to_hass(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, f"{DEVICE_KEY}_water_flow", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    _feed(entry, {**BURST, "instantrate": 412, "param38": 8, "param10": 1})
    await hass.async_block_till_done()
    assert float(_state(hass, "cop_compressor_reported").state) == 4.12
    flow = _state(hass, "water_flow_controller")
    assert float(flow.state) == 0.8 and registry.async_get(flow.entity_id).entity_category is None
