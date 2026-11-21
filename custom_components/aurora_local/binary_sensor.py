"""Binary sensors: faults, low-flow warning, compressor and setting states."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AdlarConfigEntry
from .coordinator import AdlarCoordinator
from .entity import AdlarEntity
from .pyauroralocal import fields as M

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AdlarBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[AdlarCoordinator], bool | None]
    attrs_fn: Callable[[AdlarCoordinator], dict[str, Any] | None] = lambda c: None


def _values(c: AdlarCoordinator) -> dict[str, Any]:
    return c.data.values


def _fault_on(c: AdlarCoordinator) -> bool | None:
    values = _values(c)
    if not any(k.startswith("device_fault") for k in values):
        return None
    return bool(M.fault_codes(values))


def _fault_attrs(c: AdlarCoordinator) -> dict[str, Any]:
    codes = M.fault_codes(_values(c))
    return {
        "codes": codes,
        "confirmed_codes": [x for x in codes if x in M.CONFIRMED_CODES],
        **{k: v for k, v in sorted(_values(c).items()) if k.startswith("device_fault") and v},
    }


BINARY_SENSORS: tuple[AdlarBinaryDescription, ...] = (
    AdlarBinaryDescription(
        key="fault",
        name="Fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_fault_on,
        attrs_fn=_fault_attrs,
    ),
    AdlarBinaryDescription(
        key="low_flow_warning",
        name="Low flow warning",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:water-alert",
        value_fn=lambda c: c.low_flow.active() if c.data.values else None,
        attrs_fn=lambda c: {"reason": c.low_flow.reason},
    ),
    AdlarBinaryDescription(
        key="compressor_running",
        name="Compressor running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda c: M.compressor_running(_values(c)),
    ),
    AdlarBinaryDescription(
        key="zone_1",
        name="Zone 1",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda c: M.zone_on(_values(c), 1),
    ),
    AdlarBinaryDescription(
        key="zone_2",
        name="Zone 2",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda c: M.zone_on(_values(c), 2),
    ),
    AdlarBinaryDescription(
        key="ahs",
        name="AHS (forced heating)",
        icon="mdi:fire",
        value_fn=lambda c: M.flag(_values(c), "hot"),
    ),
    AdlarBinaryDescription(
        key="holiday_mode",
        name="Holiday mode",
        icon="mdi:beach",
        value_fn=lambda c: M.flag(_values(c), "holiday"),
    ),
)


class AdlarBinarySensor(AdlarEntity, BinarySensorEntity):
    entity_description: AdlarBinaryDescription

    def __init__(self, coordinator: AdlarCoordinator, description: AdlarBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        try:
            return self.entity_description.value_fn(self.coordinator)
        except Exception:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        try:
            return self.entity_description.attrs_fn(self.coordinator)
        except Exception:
            return None


class AdlarConnectedSensor(AdlarEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Receiving data"

    def __init__(self, coordinator: AdlarCoordinator) -> None:
        super().__init__(coordinator, "connected")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.fresh


async def async_setup_entry(
    hass: HomeAssistant, entry: AdlarConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [*(AdlarBinarySensor(coordinator, d) for d in BINARY_SENSORS), AdlarConnectedSensor(coordinator)]
    )
