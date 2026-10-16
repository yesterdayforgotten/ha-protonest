"""On/off status."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .coordinator import ProtoNestConfigEntry
from .entity import ProtoNestEntity
from .pynestproto.model import Thermostat, thermostats

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class ProtoNestBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[Thermostat], bool | None]


BINARY_SENSORS: tuple[ProtoNestBinaryDescription, ...] = (
    ProtoNestBinaryDescription(
        key="filter_replacement_needed", name="Filter replacement needed",
        device_class=BinarySensorDeviceClass.PROBLEM, icon="mdi:air-filter",
        value=lambda t: t.filter_replacement_needed),
    ProtoNestBinaryDescription(key="leaf", name="Leaf", icon="mdi:leaf", value=lambda t: t.leaf),
    ProtoNestBinaryDescription(
        key="fan_running", name="Fan running", device_class=BinarySensorDeviceClass.RUNNING,
        value=lambda t: t.fan_running),
    # For run-time tracking, e.g. with the History stats helper.
    ProtoNestBinaryDescription(
        key="heating", name="Heating", device_class=BinarySensorDeviceClass.RUNNING, icon="mdi:fire",
        value=lambda t: t.hvac_action == "heating" if t.hvac_action is not None else None),
    ProtoNestBinaryDescription(
        key="cooling", name="Cooling", device_class=BinarySensorDeviceClass.RUNNING, icon="mdi:snowflake",
        value=lambda t: t.hvac_action == "cooling" if t.hvac_action is not None else None),
    ProtoNestBinaryDescription(
        key="online", name="Online", device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC, value=lambda t: t.online),  # see ProtoNestBinary
)


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(ProtoNestBinary(c, t, d) for t in thermostats(c.client.cache) for d in BINARY_SENSORS)


class ProtoNestBinary(ProtoNestEntity, BinarySensorEntity):
    entity_description: ProtoNestBinaryDescription

    def __init__(self, coordinator, thermostat, description) -> None:
        super().__init__(coordinator, thermostat, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        # Online stays available while the rest of the thermostat isn't, so
        # automations can react to it going off.
        if self.entity_description.key == "online":
            return self._connected
        return super().available

    @property
    def is_on(self) -> bool | None:
        if self.entity_description.key == "online":
            return self.coordinator.client.responsive(self.resource_id)
        return self.entity_description.value(self.device)
