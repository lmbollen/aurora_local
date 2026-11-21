"""Manual temperature calibration offsets (used when calibration is manual)."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AdlarConfigEntry
from .coordinator import AdlarCoordinator
from .entity import AdlarEntity

PARALLEL_UPDATES = 0


class AdlarOffsetNumber(AdlarEntity, NumberEntity):
    """Kelvin added to the reported inlet or outlet temperature in manual
    calibration. Changing it switches calibration to manual."""

    _attr_icon = "mdi:thermometer-plus"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.TEMPERATURE_DELTA
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_native_min_value = -5.0
    _attr_native_max_value = 5.0
    _attr_native_step = 0.05
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: AdlarCoordinator, sensor: str) -> None:
        super().__init__(coordinator, f"{sensor}_offset")
        self._sensor = sensor  # "inlet" | "outlet"
        self._attr_name = f"{sensor.capitalize()} temperature offset"

    @property
    def available(self) -> bool:
        return True  # a setting, not a reading

    @property
    def native_value(self) -> float:
        return getattr(self.coordinator.calibration, f"manual_{self._sensor}")

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_set_manual_offset(self._sensor, value)


async def async_setup_entry(
    hass: HomeAssistant, entry: AdlarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities([AdlarOffsetNumber(c, "inlet"), AdlarOffsetNumber(c, "outlet")])
