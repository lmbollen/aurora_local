"""Base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import AdlarCoordinator


class AdlarEntity(CoordinatorEntity[AdlarCoordinator]):
    """Base: one device per heat pump; available while data is fresh (< 5 min old)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AdlarCoordinator, key: str) -> None:
        super().__init__(coordinator)
        unique = coordinator.config_entry.unique_id
        self._attr_unique_id = f"{unique}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name="Adlar Castra heat pump",
        )

    @property
    def available(self) -> bool:
        return self.coordinator.fresh
