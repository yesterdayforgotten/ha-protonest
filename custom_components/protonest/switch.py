"""On/off settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .coordinator import ProtoNestConfigEntry
from .entity import ProtoNestEntity, validation_errors
from .pynestproto.model import Thermostat, structures, thermostats
from .pynestproto.write import (
    ALERTS_LABEL,
    EQUIPMENT_LABEL,
    SETTING_FLAGS,
    THRESHOLDS,
    set_fan_schedule,
    set_flag,
    set_humidifier,
    set_humidity_control,
    set_hvac_alerts,
    set_threshold,
)

PARALLEL_UPDATES = 0
SENSOR_SCHEDULE_KEY = "target_sensor_schedule"


@dataclass(frozen=True, kw_only=True)
class ProtoNestSwitchDescription(SwitchEntityDescription):
    label: str
    is_on: Callable[[Thermostat], bool | None]
    set: Callable[[Thermostat, bool], Callable]
    exists: Callable[[Thermostat], bool] = lambda t: True


def _flag(key: str, name: str) -> ProtoNestSwitchDescription:
    return ProtoNestSwitchDescription(
        key=key, name=name, entity_category=EntityCategory.CONFIG, label=SETTING_FLAGS[key][0],
        is_on=lambda t: t.flag(key), set=lambda t, on: set_flag(key, on),
        exists=lambda t: t.flag(key) is not None,
    )


def _limit_switch(key: str, name: str) -> ProtoNestSwitchDescription:
    spec = THRESHOLDS[key]

    def setter(t: Thermostat, on: bool):
        if not on:
            return set_threshold(key, None)
        _, value = t.threshold(key)
        lo, hi = (spec.min_f - 32) * 5 / 9, (spec.max_f - 32) * 5 / 9
        # Turning a limit back on: keep its value if valid, else the nearest end of its range.
        return set_threshold(key, min(max(value if value is not None else lo, lo), hi))

    return ProtoNestSwitchDescription(
        key=f"{key}_enabled", name=name, entity_category=EntityCategory.CONFIG, label=spec.label,
        is_on=lambda t: t.threshold(key)[0], set=setter,
    )


SWITCHES: tuple[ProtoNestSwitchDescription, ...] = (
    ProtoNestSwitchDescription(
        key="fan_schedule", name="Fan schedule", label="fan_control_settings",
        is_on=lambda t: t.fan_schedule_enabled, set=lambda t, on: set_fan_schedule(enabled=on),
        exists=lambda t: t.has_fan,
    ),
    _flag("auto_schedule", "Auto-Schedule"),
    _flag("home_away_assist", "Home/Away Assist"),
    _flag("cool_to_dry", "Cool to Dry"),
    _flag("sunblock", "Sunblock"),
    _flag("airwave", "Airwave"),
    _flag("wake_on_approach", "Wake on approach"),
    _flag("filter_reminder", "Filter reminder"),
    ProtoNestSwitchDescription(
        key="humidity_control", name="Humidity control", label="humidity_control_settings",
        is_on=lambda t: t.humidity_control_enabled, set=lambda t, on: set_humidity_control(on),
        exists=lambda t: t.has_humidifier or t.has_dehumidifier,
    ),
    ProtoNestSwitchDescription(
        key="humidifier_fan", name="Run fan while humidifying", entity_category=EntityCategory.CONFIG,
        label=EQUIPMENT_LABEL, is_on=lambda t: t.humidifier_runs_fan,
        set=lambda t, on: set_humidifier(run_fan=on), exists=lambda t: t.has_humidifier,
    ),
    _limit_switch("eco_heat", "Eco heating limit"),
    _limit_switch("eco_cool", "Eco cooling limit"),
    _limit_switch("safety_low", "Safety low limit"),
    _limit_switch("safety_high", "Safety high limit"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(
        [ProtoNestSwitch(c, t, d) for t in thermostats(c.client.cache) for d in SWITCHES if d.exists(t)]
        + [SensorScheduleSwitch(c, t) for t in thermostats(c.client.cache)]
        + [HvacAlertsSwitch(c, h) for h in structures(c.client.cache) if h.hvac_alerts is not None]
    )


class ProtoNestSwitch(ProtoNestEntity, SwitchEntity):
    entity_description: ProtoNestSwitchDescription

    def __init__(self, coordinator, thermostat, description: ProtoNestSwitchDescription) -> None:
        super().__init__(coordinator, thermostat, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on(self.device)

    @validation_errors
    async def async_turn_on(self, **kwargs: Any) -> None:
        d = self.entity_description
        await self.write(d.label, d.set(self.device, True))

    @validation_errors
    async def async_turn_off(self, **kwargs: Any) -> None:
        d = self.entity_description
        await self.write(d.label, d.set(self.device, False))


class SensorScheduleSwitch(ProtoNestEntity, SwitchEntity):
    """Runs this thermostat's Home Assistant target sensor schedule (sensor_schedule.py)."""

    _attr_name = "Target sensor schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator, thermostat) -> None:
        super().__init__(coordinator, thermostat, SENSOR_SCHEDULE_KEY)

    @property
    def is_on(self) -> bool:
        return self.coordinator.sensor_schedule.enabled(self.resource_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entries": len(self.coordinator.sensor_schedule.entries(self.resource_id))}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.sensor_schedule.async_set_enabled(self.resource_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.sensor_schedule.async_set_enabled(self.resource_id, False)


class HvacAlertsSwitch(ProtoNestEntity, SwitchEntity):
    """Nest's HVAC monitoring alerts for the home: a heads-up when heating or
    cooling doesn't seem to be working as it should."""

    _attr_name = "HVAC alerts"
    _attr_icon = "mdi:hvac"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, home) -> None:
        super().__init__(coordinator, home, "hvac_alerts")

    @property
    def is_on(self) -> bool | None:
        return self.device.hvac_alerts

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.write(ALERTS_LABEL, set_hvac_alerts(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.write(ALERTS_LABEL, set_hvac_alerts(False))
