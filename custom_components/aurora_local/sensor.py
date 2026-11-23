"""Sensors for the Adlar Castra heat pump.

Known fields use the meanings confirmed in docs/adlar-castra-protocol.md
(names as in the Thermomax app, units checked against counters). Every other
field the heat pump reports gets a generic entity automatically, except known
duplicates. Parameter tables (the E2 installer table) get one entity each.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import AdlarConfigEntry
from .const import SIGNAL_NEW_KEYS, SIGNAL_NEW_TABLES
from .coordinator import AdlarCoordinator
from .entity import AdlarEntity
from .pyauroralocal import STATUSES
from .pyauroralocal import fields as M
from .pyauroralocal.calibration import CAL_MIN_SAMPLES
from .pyauroralocal.state import to_signed16

_LOGGER = logging.getLogger(__name__)

Values = dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class AdlarSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Values], Any] = lambda v: None
    # When set, used instead of value_fn (needs coordinator state, e.g. calibration)
    coord_fn: Callable[[AdlarCoordinator], Any] | None = None
    attrs_fn: Callable[[AdlarCoordinator], dict[str, Any] | None] | None = None
    sources: tuple[str, ...] = ()  # raw fields this sensor covers


PARALLEL_UPDATES = 0  # push data; nothing to throttle


def _cal(c: AdlarCoordinator, key: str) -> float | None:
    """Calibrated value of the latest burst (temp_in, temp_out, delta_t, heat, cop)."""
    return c.corrected.get(key)


def _offset(c: AdlarCoordinator) -> float:
    """Correction applied to (outlet − inlet), e.g. -0.86 K."""
    add_in, add_out = c.calibration.offsets(time.time())
    return round(add_out - add_in, 2)


def _temp(key: str, field: str, name: str, **kw: Any) -> AdlarSensorDescription:
    return AdlarSensorDescription(
        key=key,
        name=name,
        sources=(field,),
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda v, f=field: M.temp(v, f),
        **kw,
    )


def _energy(key: str, field: str, name: str, **kw: Any) -> AdlarSensorDescription:
    return AdlarSensorDescription(
        key=key,
        name=name,
        sources=(field,),
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda v, f=field: M.counter_kwh(v, f),
        **kw,
    )


def _raw(key: str, field: str, name: str, **kw: Any) -> AdlarSensorDescription:
    return AdlarSensorDescription(
        key=key,
        name=name,
        sources=(field,),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda v, f=field: M.num(v, f),
        **kw,
    )


DIAG = EntityCategory.DIAGNOSTIC

SENSORS: tuple[AdlarSensorDescription, ...] = (
    # Temperatures (confirmed against the app's Device Details page)
    # Inlet/outlet: calibrated values by default; the pump's own readings are
    # in the "(reported)" twins under Diagnostic.
    _temp("temp_in", "temp_in", "Return water temperature",
          coord_fn=lambda c: _cal(c, "temp_in")),
    _temp("temp_out", "temp_out", "Leaving water temperature",
          coord_fn=lambda c: _cal(c, "temp_out")),
    _temp("temp_in_reported", "temp_in", "Return water temperature (reported)", entity_category=DIAG),
    _temp("temp_out_reported", "temp_out", "Leaving water temperature (reported)", entity_category=DIAG),
    _temp("outdoor_temperature", "temp_envir2", "Outdoor temperature"),
    _temp("room_temperature", "temp_envir1", "Room temperature"),
    _temp("dhw_tank_temperature", "temp_box", "DHW tank temperature"),
    _temp("target_flow_temperature", "temp_curve2", "Target flow temperature"),
    # Meaning not confirmed (constant 24 °C so far; temp_curve12 follows the target)
    _temp("temp_curve1", "temp_curve1", "Curve temperature 1 (raw)", entity_category=DIAG),
    _temp("temp_curve11", "temp_curve11", "Curve temperature 11 (raw)", entity_category=DIAG),
    _temp("temp_curve12", "temp_curve12", "Curve temperature 12 (raw)", entity_category=DIAG),
    AdlarSensorDescription(
        key="delta_t",
        name="Flow/return difference",
        device_class=SensorDeviceClass.TEMPERATURE_DELTA,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        icon="mdi:thermometer-lines",
        suggested_display_precision=1,
        coord_fn=lambda c: _cal(c, "delta_t"),
    ),
    AdlarSensorDescription(
        key="delta_t_reported",
        name="Flow/return difference (reported)",
        device_class=SensorDeviceClass.TEMPERATURE_DELTA,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        icon="mdi:thermometer-lines",
        suggested_display_precision=1,
        entity_category=DIAG,
        value_fn=M.delta_t,
    ),
    # Power and heat
    AdlarSensorDescription(
        key="instantpower",
        name="Electrical power",
        sources=("instantpower",),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=M.electrical_power,
    ),
    # Heat and COP: calibrated (heat scaled by calibrated ΔT / reported ΔT,
    # since the pump computes heat as flow × ΔT). Pump's own figures below.
    AdlarSensorDescription(
        key="heat_output",
        name="Heat output",
        sources=("energy2",),
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        coord_fn=lambda c: _cal(c, "heat"),
    ),
    AdlarSensorDescription(
        key="cop",
        name="COP",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        coord_fn=lambda c: _cal(c, "cop"),
    ),
    AdlarSensorDescription(
        key="heat_output_reported",
        name="Heat output (reported)",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=DIAG,
        value_fn=M.heat_output,
    ),
    AdlarSensorDescription(
        key="cop_reported",
        name="COP (reported)",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=DIAG,
        value_fn=M.cop,
    ),
    AdlarSensorDescription(
        key="heat_produced_calibrated",
        name="Heat produced since install (corrected)",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        coord_fn=lambda c: round(c.heat_energy.kwh, 3),
    ),
    AdlarSensorDescription(
        key="temperature_offset",
        name="Temperature calibration offset",
        device_class=SensorDeviceClass.TEMPERATURE_DELTA,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        suggested_display_precision=2,
        icon="mdi:tune-vertical",
        entity_category=DIAG,
        coord_fn=_offset,
        attrs_fn=lambda c: {
            "mode": c.calibration.mode,
            "auto_offset_measured": c.calibration.auto_offset(time.time()),
            "auto_samples_7d": len(c.calibration.recent(time.time())),
            "auto_min_samples": CAL_MIN_SAMPLES,
            "manual_inlet_offset": c.calibration.manual_inlet,
            "manual_outlet_offset": c.calibration.manual_outlet,
        },
    ),
    _energy("electricity_consumed", "power_use", "Electricity consumed"),
    _energy("heat_produced", "energy_use2", "Heat produced, lifetime (reported)", entity_category=DIAG),
    _energy("electricity_consumed_12", "power_use12", "Electricity consumed (subset)", entity_category=DIAG,
            entity_registry_enabled_default=False),
    AdlarSensorDescription(
        key="compressor_run_time",
        name="Compressor run time",
        sources=("runtime2",),
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        value_fn=lambda v: M.num(v, "runtime2"),
    ),
    AdlarSensorDescription(
        key="compressor_frequency",
        name="Compressor frequency",
        sources=("param1",),
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        value_fn=M.compressor_hz,
    ),
    AdlarSensorDescription(
        key="water_flow",
        name="Water flow (raw)",
        sources=("instantrate",),
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-pump",
        value_fn=lambda v: M.num(v, "instantrate"),
    ),
    AdlarSensorDescription(
        key="voltage1",
        name="Voltage",
        sources=("voltage1",),
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda v: M.num(v, "voltage1"),
    ),
    # Settings / state
    AdlarSensorDescription(
        key="quiet_mode",
        name="Quiet mode",
        sources=("functionadd",),
        device_class=SensorDeviceClass.ENUM,
        options=list(M.QUIET_MODES.values()),
        icon="mdi:sleep",
        value_fn=M.quiet_mode,
    ),
    AdlarSensorDescription(
        key="fault_code",
        name="Fault code",
        icon="mdi:alert-circle-outline",
        # "none" once fault data has arrived; unknown before that
        value_fn=lambda v: (", ".join(M.fault_codes(v)) or "none")
        if any(k.startswith("device_fault") for k in v) else None,
    ),
    _raw("mode", "mode", "Mode (raw)"),
    _raw("dmode1", "dmode1", "Mode 1 (raw)"),
    # Refrigerant circuit and drives: identified from the data (docs §5),
    # not shown by the vendor app. At standstill both pressures equalise.
    AdlarSensorDescription(
        key="high_pressure",
        name="Refrigerant high pressure",
        sources=(M.HIGH_PRESSURE,),
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.BAR,
        suggested_display_precision=1,
        entity_category=DIAG,
        value_fn=lambda v: M.pressure_bar(v, M.HIGH_PRESSURE),
    ),
    AdlarSensorDescription(
        key="low_pressure",
        name="Refrigerant low pressure",
        sources=(M.LOW_PRESSURE,),
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.BAR,
        suggested_display_precision=1,
        entity_category=DIAG,
        value_fn=lambda v: M.pressure_bar(v, M.LOW_PRESSURE),
    ),
    _temp("evaporating_temperature", M.EVAPORATING_TEMP, "Evaporating temperature", entity_category=DIAG),
    _temp("discharge_temperature", M.DISCHARGE_TEMP, "Compressor discharge temperature", entity_category=DIAG),
    _temp("suction_temperature", M.SUCTION_TEMP, "Compressor suction temperature", entity_category=DIAG),
    _temp("coil_temperature", M.COIL_TEMP, "Outdoor coil temperature", entity_category=DIAG),
    _temp("liquid_line_temperature", M.LIQUID_LINE_TEMP, "Liquid line temperature", entity_category=DIAG),
    AdlarSensorDescription(
        key="compressor_current",
        name="Compressor current",
        sources=(M.COMPRESSOR_CURRENT,),
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        entity_category=DIAG,
        value_fn=lambda v: M.tenths(v, M.COMPRESSOR_CURRENT),
    ),
    AdlarSensorDescription(
        key="water_flow_controller",
        name="Water flow (controller)",
        sources=(M.WATER_FLOW_2,),
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
        suggested_display_precision=1,
        entity_category=DIAG,
        value_fn=lambda v: M.tenths(v, M.WATER_FLOW_2),
    ),
    AdlarSensorDescription(
        key="dc_bus_voltage",
        name="DC bus voltage",
        sources=(M.DC_BUS_VOLTAGE,),
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_category=DIAG,
        value_fn=lambda v: M.num(v, M.DC_BUS_VOLTAGE),
    ),
    AdlarSensorDescription(
        key="expansion_valve",
        name="Expansion valve opening",
        sources=(M.EXPANSION_VALVE,),
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="steps",
        icon="mdi:valve",
        entity_category=DIAG,
        value_fn=lambda v: M.num(v, M.EXPANSION_VALVE),
    ),
    AdlarSensorDescription(
        key="outdoor_fan_speed",
        name="Outdoor fan speed",
        sources=(M.OUTDOOR_FAN,),
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        icon="mdi:fan",
        entity_category=DIAG,
        value_fn=lambda v: M.num(v, M.OUTDOOR_FAN),
    ),
    AdlarSensorDescription(
        key="circulation_pump_speed",
        name="Circulation pump speed",
        sources=(M.CIRCULATION_PUMP,),
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:pump",
        entity_category=DIAG,
        value_fn=lambda v: M.num(v, M.CIRCULATION_PUMP),
    ),
)

# Raw fields already covered by a sensor above, by a binary sensor, or that
# duplicate another field (docs §5, checked on 19 days of data). No generic
# entity for these. param9, param10, param53 and param59 are NOT duplicates.
COVERED = {s for d in SENSORS for s in d.sources} | {
    "onoff", "onoff1", "hot", "holiday", "fixtime", "bandUser",
    "param3", "param4", "param17", "param29", "param30", "param48", "param50",
    "param57", "param47", "param49",
    M.FOUR_WAY_VALVE, M.OIL_RETURN,  # Defrost / Oil return binary sensors
}
SKIP_KEY_RE = re.compile(r"^device_fault\d+$")


class AdlarSensor(AdlarEntity, SensorEntity):
    entity_description: AdlarSensorDescription

    def __init__(self, coordinator: AdlarCoordinator, description: AdlarSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        d = self.entity_description
        try:
            if d.coord_fn is not None:
                return d.coord_fn(self.coordinator)
            return d.value_fn(self.coordinator.data.values)
        except Exception:  # a format change must not break the entity
            _LOGGER.debug("Could not compute %s", d.key, exc_info=True)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        try:
            return self.entity_description.attrs_fn(self.coordinator)
        except Exception:
            return None


class AdlarGenericSensor(AdlarEntity, SensorEntity):
    """Any other field, shown exactly as received."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AdlarCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._key = key
        self._attr_name = key
        # Fields whose meaning isn't known: available but off by default, so
        # the device page isn't buried in raw values. Enable any you need.
        self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> Any:
        value = self.coordinator.data.values.get(self._key)
        if isinstance(value, (dict, list)):
            return str(value)[:255]
        if isinstance(value, str):
            return value[:255]
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        value = self.coordinator.data.values.get(self._key)
        signed = to_signed16(value)
        return {"as_signed16": signed} if signed != value else None


class AdlarTableSensor(AdlarEntity, SensorEntity):
    """A parameter table (e.g. E2 installer settings) as attributes."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"parameters"})

    def __init__(self, coordinator: AdlarCoordinator, table: str) -> None:
        super().__init__(coordinator, "e2_parameters" if table == "E2" else f"table_{table}")
        self._table = table
        self._attr_name = "Installer parameters" if table == "E2" else f"{table} parameters"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.tables.get(self._table, {}))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        table = self.coordinator.data.tables.get(self._table, {})
        return {"parameters": {f"P{k}": v for k, v in sorted(table.items())}}


class AdlarConnectionSensor(AdlarEntity, SensorEntity):
    """Connection state of the local link, with retry details."""

    _attr_name = "Connection"
    _attr_icon = "mdi:lan"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AdlarCoordinator) -> None:
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.client.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator.client
        ts = lambda t: dt_util.utc_from_timestamp(t).isoformat() if t else None  # noqa: E731
        return {
            "host": f"{c.host}:{c.port}",
            "receiving_data": self.coordinator.fresh,
            "failed_attempts": c.failures,
            "last_error": c.last_error,
            "next_attempt": ts(c.next_attempt),
            "last_connected": ts(c.last_connected),
            "last_disconnected": ts(c.last_disconnected),
            "last_search": c.last_scan_result,
        }


async def async_setup_entry(
    hass: HomeAssistant, entry: AdlarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    created_keys: set[str] = set(COVERED)
    created_tables: set[str] = set()

    @callback
    def add_keys(keys: set[str]) -> None:
        new = {k for k in keys - created_keys if not SKIP_KEY_RE.match(k)}
        created_keys.update(keys)
        if new:
            async_add_entities(AdlarGenericSensor(coordinator, k) for k in sorted(new))

    @callback
    def add_tables(tables: set[str]) -> None:
        new = tables - created_tables
        created_tables.update(new)
        if new:
            async_add_entities(AdlarTableSensor(coordinator, t) for t in sorted(new))

    async_add_entities([*(AdlarSensor(coordinator, d) for d in SENSORS), AdlarConnectionSensor(coordinator)])

    # A field that now has a named sensor no longer gets a generic entity;
    # remove the generic one an older version created, so it doesn't linger
    # as "no longer provided".
    registry = er.async_get(hass)
    prefix = f"{entry.unique_id}_"
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain != "sensor" or not reg.unique_id.startswith(prefix):
            continue
        key = reg.unique_id.removeprefix(prefix)
        if key in COVERED and key.startswith("param") and key not in {d.key for d in SENSORS}:
            registry.async_remove(reg.entity_id)

    entry_id = entry.entry_id
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_KEYS.format(entry_id), add_keys))
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_TABLES.format(entry_id), add_tables))
    add_keys(set(coordinator.seen_keys))
    add_tables(set(coordinator.seen_tables) | {"E2"})
