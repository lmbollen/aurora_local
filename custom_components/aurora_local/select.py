"""Temperature calibration mode (auto / manual)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AdlarConfigEntry
from .coordinator import AdlarCoordinator
from .entity import AdlarEntity
from .pyauroralocal import CAL_AUTO, CAL_MANUAL

PARALLEL_UPDATES = 0


class AdlarCalibrationModeSelect(AdlarEntity, SelectEntity):
    """auto: learn the inlet/outlet offset while the pump is off (default).
    manual: use the Inlet/Outlet temperature offset numbers."""

    _attr_name = "Temperature calibration"
    _attr_icon = "mdi:tune-vertical"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [CAL_AUTO, CAL_MANUAL]

    def __init__(self, coordinator: AdlarCoordinator) -> None:
        super().__init__(coordinator, "calibration_mode")

    @property
    def available(self) -> bool:
        return True  # a setting, not a reading

    @property
    def current_option(self) -> str:
        return self.coordinator.calibration.mode

    async def async_select_option(self, option: str) -> None:
        self.coordinator.async_set_calibration_mode(option)


async def async_setup_entry(
    hass: HomeAssistant, entry: AdlarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AdlarCalibrationModeSelect(entry.runtime_data)])
