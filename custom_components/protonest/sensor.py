"""Readings and status."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricPotential, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant

from .coordinator import ProtoNestConfigEntry
from .entity import ProtoNestEntity
from .pynestproto.model import remote_sensors, thermostats

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class ProtoNestSensorDescription(SensorEntityDescription):
    value: Callable[[object], float | str | datetime | None]


THERMOSTAT_SENSORS: tuple[ProtoNestSensorDescription, ...] = (
    ProtoNestSensorDescription(
        key="humidity", name="Humidity", device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0, value=lambda t: t.current_humidity),
    ProtoNestSensorDescription(
        key="temperature", name="Temperature", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, value=lambda t: t.current_temperature),
    ProtoNestSensorDescription(
        key="backplate_temperature", name="Thermostat sensor temperature", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, value=lambda t: t.backplate_temperature),
    ProtoNestSensorDescription(
        key="filter_runtime", name="Filter runtime", native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING, suggested_display_precision=0, icon="mdi:air-filter",
        value=lambda t: t.filter_runtime_hours),
    ProtoNestSensorDescription(
        key="filter_reminder_level", name="Filter reminder level", icon="mdi:air-filter",
        value=lambda t: t.filter_reminder_level),
    ProtoNestSensorDescription(
        key="filter_changed", name="Filter last changed", device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:air-filter", value=lambda t: t.filter_changed),
    ProtoNestSensorDescription(
        key="time_to_target", name="Time to target", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value=lambda t: round(t.time_to_target / 60) if t.time_to_target else None),
    ProtoNestSensorDescription(
        key="compressor_lockout", name="Compressor lockout until", device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda t: t.compressor_lockout_until),
    ProtoNestSensorDescription(
        key="running", name="Running", icon="mdi:hvac",
        value=lambda t: ", ".join(s.removesuffix("Active") for s in t.active_stages) or "idle"),
    ProtoNestSensorDescription(
        key="battery_voltage", name="Battery voltage", device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT, entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2, value=lambda t: t.battery_voltage),
)

REMOTE_SENSORS: tuple[ProtoNestSensorDescription, ...] = (
    ProtoNestSensorDescription(
        key="temperature", name="Temperature", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1, icon="protonest:sensor", value=lambda s: s.temperature),
    ProtoNestSensorDescription(
        key="battery_voltage", name="Battery voltage", device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT, entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2, value=lambda s: s.battery_voltage),
)


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry, async_add_entities) -> None:
    c = entry.runtime_data
    cache = c.client.cache
    async_add_entities(
        [ProtoNestSensor(c, t, d) for t in thermostats(cache) for d in THERMOSTAT_SENSORS]
        + [ProtoNestSensor(c, s, d) for s in remote_sensors(cache) for d in REMOTE_SENSORS]
    )


class ProtoNestSensor(ProtoNestEntity, SensorEntity):
    entity_description: ProtoNestSensorDescription

    def __init__(self, coordinator, device, description) -> None:
        super().__init__(coordinator, device, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value(self.device)
