"""Reconnect button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AdlarConfigEntry
from .coordinator import AdlarCoordinator
from .entity import AdlarEntity

PARALLEL_UPDATES = 0


class AdlarReconnectButton(AdlarEntity, ButtonEntity):
    """Retry the connection now and reset the backoff."""

    _attr_name = "Reconnect now"
    _attr_icon = "mdi:lan-connect"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AdlarCoordinator) -> None:
        super().__init__(coordinator, "reconnect")

    @property
    def available(self) -> bool:
        return True  # most useful exactly when the heat pump is unreachable

    async def async_press(self) -> None:
        self.coordinator.async_reconnect()


async def async_setup_entry(
    hass: HomeAssistant, entry: AdlarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AdlarReconnectButton(entry.runtime_data)])
