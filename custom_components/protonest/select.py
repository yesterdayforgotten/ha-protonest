"""Choice settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .coordinator import ProtoNestConfigEntry
from .entity import ProtoNestEntity, validation_errors
from .pynestproto.model import SENSOR_AVERAGE, Thermostat, thermostats
from .pynestproto.write import (
    DISPLAY_LABEL,
    EARLY_ON_LABEL,
    EQUIPMENT_LABEL,
    FAN_RUN_TIMES,
    FAN_TIMER_DURATIONS,
    SENSOR_PERIODS,
    SENSOR_SCHEDULE,
    THERMOSTAT_SENSOR,
    select_sensor,
    set_early_on,
    set_fan_run_duration,
    set_fan_schedule,
    set_farsight,
    set_humidifier,
    set_multi_sensor,
    set_sensor_schedule,
    set_temperature_scale,
)

PARALLEL_UPDATES = 0

SENSOR_LABEL = "remote_comfort_sensing_settings"
FAN_LABEL = "fan_control_settings"
OPT_THERMOSTAT, OPT_SCHEDULE, OPT_AVERAGE = "Thermostat", "Schedule", "Average of all sensors"
HOURS = [f"{h:02d}:00" for h in range(24)]
DURATION_NAMES = {900: "15 min", 1800: "30 min", 2700: "45 min", 3600: "1 hour", 7200: "2 hours",
                  14400: "4 hours", 28800: "8 hours", 43200: "12 hours", 86400: "1 day",
                  259200: "3 days", 432000: "5 days", 604800: "1 week"}
FARSIGHT_NAMES = {"target": "Target temperature", "current": "Current temperature", "digital_clock": "Digital clock",
                  "analog_clock": "Analog clock", "weather": "Weather", "none": "Nothing"}
HUMIDIFIER_NAMES = {"bypass": "Only with heat", "steam": "Any time"}


@dataclass(frozen=True, kw_only=True)
class ProtoNestSelectDescription(SelectEntityDescription):
    label: str
    current: Callable[["ProtoNestSelect"], str | None]
    choices: Callable[["ProtoNestSelect"], list[str]]
    set: Callable[["ProtoNestSelect", str], Callable]
    exists: Callable[[Thermostat], bool] = lambda t: True


def _inverse(mapping: dict, option: str):
    return next(k for k, v in mapping.items() if v == option)


# --- target sensor --------------------------------------------------------------------


def _sensor_labels(sel: "ProtoNestSelect") -> dict[str, str]:
    """Paired sensor ID -> option label, unique even when two sensors share a name."""
    cache = sel.coordinator.client.cache
    names = {s: cache.display_name(s) for s in sel.device.paired_sensors}
    dupes = {n for n in names.values() if list(names.values()).count(n) > 1}
    return {s: f"{n} ({s[-4:]})" if n in dupes else n for s, n in names.items()}


def _sensor_name(sel: "ProtoNestSelect", choice: str | None) -> str | None:
    if choice == THERMOSTAT_SENSOR:
        return OPT_THERMOSTAT
    if choice == SENSOR_SCHEDULE:
        return OPT_SCHEDULE
    if choice == SENSOR_AVERAGE:
        return OPT_AVERAGE
    if not choice:
        return None
    return _sensor_labels(sel).get(choice) or sel.coordinator.client.cache.display_name(choice)


def _sensor_choices(sel: "ProtoNestSelect", with_modes: bool) -> list[str]:
    t = sel.device
    opts = [OPT_THERMOSTAT, *_sensor_labels(sel).values()]
    if with_modes and len(t.paired_sensors) >= 2:
        opts.append(OPT_AVERAGE)
    if with_modes and t.has_sensor_schedule:
        opts.append(OPT_SCHEDULE)
    return opts


def _sensor_id(sel: "ProtoNestSelect", option: str) -> str:
    if option == OPT_THERMOSTAT:
        return THERMOSTAT_SENSOR
    return next(s for s, label in _sensor_labels(sel).items() if label == option)


def _set_target_sensor(sel: "ProtoNestSelect", option: str):
    if option == OPT_SCHEDULE:
        return select_sensor(SENSOR_SCHEDULE)
    if option == OPT_AVERAGE:
        return set_multi_sensor(sel.device.paired_sensors)
    return select_sensor(_sensor_id(sel, option))


def _period(period: str) -> ProtoNestSelectDescription:
    return ProtoNestSelectDescription(
        key=f"sensor_{period}", name=f"Sensor schedule: {period}", entity_category=EntityCategory.CONFIG,
        label=SENSOR_LABEL, exists=lambda t: t.has_sensor_schedule,
        current=lambda sel: _sensor_name(sel, sel.device.sensor_for_period(period)),
        choices=lambda sel: _sensor_choices(sel, with_modes=False),
        set=lambda sel, option: set_sensor_schedule({period: _sensor_id(sel, option)}),
    )


SELECTS: tuple[ProtoNestSelectDescription, ...] = (
    ProtoNestSelectDescription(
        key="target_sensor", name="Target sensor", label=SENSOR_LABEL, icon="mdi:thermometer-check",
        exists=lambda t: bool(t.paired_sensors),
        current=lambda sel: _sensor_name(sel, sel.device.active_sensor),
        choices=lambda sel: _sensor_choices(sel, with_modes=True),
        set=_set_target_sensor,
    ),
    *(_period(p) for p in SENSOR_PERIODS),
    ProtoNestSelectDescription(
        key="fan_schedule_run_time", name="Fan schedule run time", entity_category=EntityCategory.CONFIG,
        label=FAN_LABEL, exists=lambda t: t.has_fan,
        current=lambda sel: f"{sel.device.fan_schedule_minutes} min per hour" if sel.device.fan_schedule_minutes else None,
        choices=lambda sel: [f"{s // 60} min per hour" for s in FAN_RUN_TIMES],
        set=lambda sel, o: set_fan_schedule(run_seconds_per_hour=int(o.split()[0]) * 60),
    ),
    ProtoNestSelectDescription(
        key="fan_schedule_start", name="Fan schedule start", entity_category=EntityCategory.CONFIG,
        label=FAN_LABEL, exists=lambda t: t.has_fan,
        current=lambda sel: HOURS[sel.device.fan_schedule_hours[0]] if sel.device.fan_schedule_hours else None,
        choices=lambda sel: HOURS, set=lambda sel, o: set_fan_schedule(start_hour=HOURS.index(o)),
    ),
    ProtoNestSelectDescription(
        key="fan_schedule_end", name="Fan schedule end", entity_category=EntityCategory.CONFIG,
        label=FAN_LABEL, exists=lambda t: t.has_fan,
        current=lambda sel: HOURS[sel.device.fan_schedule_hours[1]] if sel.device.fan_schedule_hours else None,
        choices=lambda sel: HOURS, set=lambda sel, o: set_fan_schedule(end_hour=HOURS.index(o)),
    ),
    ProtoNestSelectDescription(
        key="fan_run_duration", name="Fan run duration", entity_category=EntityCategory.CONFIG,
        label=FAN_LABEL, exists=lambda t: t.has_fan,
        current=lambda sel: DURATION_NAMES.get(sel.device.fan_run_duration),
        choices=lambda sel: [DURATION_NAMES[d] for d in FAN_TIMER_DURATIONS],
        set=lambda sel, o: set_fan_run_duration(_inverse(DURATION_NAMES, o)),
    ),
    ProtoNestSelectDescription(
        key="early_on", name="Early-On", entity_category=EntityCategory.CONFIG, label=EARLY_ON_LABEL,
        current=lambda sel: "Off" if sel.device.early_on_hours is None else f"{sel.device.early_on_hours} h",
        choices=lambda sel: ["Off", *(f"{h} h" for h in range(6))],
        set=lambda sel, o: set_early_on(None if o == "Off" else int(o.split()[0])),
    ),
    ProtoNestSelectDescription(
        key="farsight", name="Farsight display", entity_category=EntityCategory.CONFIG, label=DISPLAY_LABEL,
        current=lambda sel: FARSIGHT_NAMES.get(sel.device.farsight),
        choices=lambda sel: list(FARSIGHT_NAMES.values()),
        set=lambda sel, o: set_farsight(_inverse(FARSIGHT_NAMES, o)),
    ),
    ProtoNestSelectDescription(
        key="display_units", name="Display units", entity_category=EntityCategory.CONFIG, label=DISPLAY_LABEL,
        current=lambda sel: f"°{sel.device.temperature_scale}", choices=lambda sel: ["°C", "°F"],
        set=lambda sel, o: set_temperature_scale(o[-1]),
    ),
    ProtoNestSelectDescription(
        key="humidifier_type", name="Humidifier operation", entity_category=EntityCategory.CONFIG,
        label=EQUIPMENT_LABEL, exists=lambda t: t.has_humidifier,
        current=lambda sel: HUMIDIFIER_NAMES.get(sel.device.humidifier_type),
        choices=lambda sel: list(HUMIDIFIER_NAMES.values()),
        set=lambda sel, o: set_humidifier(kind=_inverse(HUMIDIFIER_NAMES, o)),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(ProtoNestSelect(c, t, d) for t in thermostats(c.client.cache) for d in SELECTS if d.exists(t))


class ProtoNestSelect(ProtoNestEntity, SelectEntity):
    entity_description: ProtoNestSelectDescription

    def __init__(self, coordinator, thermostat, description) -> None:
        super().__init__(coordinator, thermostat, description.key)
        self.entity_description = description

    @property
    def options(self) -> list[str]:
        return self.entity_description.choices(self)

    @property
    def current_option(self) -> str | None:
        return self.entity_description.current(self)

    @validation_errors
    async def async_select_option(self, option: str) -> None:
        d = self.entity_description
        await self.write(d.label, d.set(self, option))
