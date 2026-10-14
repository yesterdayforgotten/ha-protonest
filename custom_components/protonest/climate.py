"""Climate entity per thermostat."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    FAN_AUTO,
    FAN_ON,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, PRECISION_WHOLE, UnitOfTemperature
from datetime import time as dtime, timedelta

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform, entity_registry as er

from .const import DOMAIN
from .coordinator import ProtoNestConfigEntry
from .sensor_schedule import Entry, validate
from .switch import SENSOR_SCHEDULE_KEY
from .entity import ProtoNestEntity, validation_errors
from .pynestproto.model import SENSOR_TYPE, thermostats
from .pynestproto.write import (
    HUMIDITY_MAX,
    HUMIDITY_MIN,
    HUMIDITY_STEP,
    SCHEDULE_LABELS,
    SCHEDULE_MAX_F,
    SCHEDULE_MIN_F,
    THERMOSTAT_SENSOR,
    DAYS,
    add_schedule_entry,
    check_target_temperature,
    pair_sensor,
    remove_schedule_entry,
    run_fan,
    set_eco,
    set_hvac_mode,
    set_multi_sensor,
    set_target_humidity,
    set_target_temperature,
    stop_fan,
    unpair_sensor,
    update_schedule_entry,
)

PARALLEL_UPDATES = 0

TARGET = "target_temperature_settings"
_TO_HA = {"off": HVACMode.OFF, "heat": HVACMode.HEAT, "cool": HVACMode.COOL, "range": HVACMode.HEAT_COOL}
_FROM_HA = {v: k for k, v in _TO_HA.items()}
_ACTIONS = {"off": HVACAction.OFF, "idle": HVACAction.IDLE, "heating": HVACAction.HEATING,
            "cooling": HVACAction.COOLING, "fan": HVACAction.FAN}
# Used only when switching to a mode whose setpoint was never stored (e.g. a
# thermostat only ever used for cooling has no heating setpoint).
_DEFAULT_HEAT_C, _DEFAULT_COOL_C = 20.0, 25.5
_DEFAULT_FAN_RUN = 3600


_SCHEDULE = vol.In(list(SCHEDULE_LABELS))
_DAY = vol.In(list(DAYS))
_SENSOR_ENTRY = vol.Schema({
    vol.Required("day"): _DAY,
    vol.Required("time"): cv.time,
    vol.Required("sensors"): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1)),
})
_TEMPS = {
    vol.Optional("temperature"): vol.Coerce(float),
    vol.Optional("target_temp_low"): vol.Coerce(float),
    vol.Optional("target_temp_high"): vol.Coerce(float),
}


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry, async_add_entities) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ProtoNestClimate(coordinator, t) for t in thermostats(coordinator.client.cache))

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "run_fan", {vol.Required("duration"): cv.positive_time_period}, "async_run_fan")
    platform.async_register_entity_service(
        "average_sensors", {vol.Required("sensors"): cv.entity_ids}, "async_average_sensors")
    platform.async_register_entity_service("pair_sensor", {vol.Required("sensor"): cv.entity_id}, "async_pair_sensor")
    platform.async_register_entity_service("unpair_sensor", {vol.Required("sensor"): cv.entity_id}, "async_unpair_sensor")
    platform.async_register_entity_service(
        "schedule_add",
        {vol.Required("schedule"): _SCHEDULE, vol.Required("day"): _DAY, vol.Required("time"): cv.time, **_TEMPS},
        "async_schedule_add")
    platform.async_register_entity_service(
        "schedule_update",
        {vol.Required("schedule"): _SCHEDULE, vol.Required("day"): _DAY, vol.Required("time"): cv.time,
         vol.Optional("new_day"): _DAY, vol.Optional("new_time"): cv.time, **_TEMPS},
        "async_schedule_update")
    platform.async_register_entity_service(
        "set_sensor_schedule", {vol.Required("entries"): vol.All(cv.ensure_list, [_SENSOR_ENTRY])},
        "async_set_sensor_schedule")
    platform.async_register_entity_service(
        "schedule_remove",
        {vol.Required("schedule"): _SCHEDULE, vol.Required("day"): _DAY, vol.Required("time"): cv.time},
        "async_schedule_remove")


def climate_name(device_name: str | None) -> str | None:
    """The climate entity's name: a thermostat named "Living Room" becomes "Living
    Room Thermostat" (climate.living_room_thermostat), but one already named
    "Hallway Thermostat" keeps that name rather than doubling it."""
    return None if (device_name or "").lower().endswith("thermostat") else "Thermostat"


class ProtoNestClimate(ProtoNestEntity, ClimateEntity):
    _attr_icon = "protonest:thermostat"  # frontend/protonest-icons.js
    _unrecorded_attributes = frozenset({"schedule", "sensor_schedule"})  # large and rarely change
    _attr_preset_modes = [PRESET_NONE, PRESET_ECO]
    _attr_fan_modes = [FAN_AUTO, FAN_ON]

    def __init__(self, coordinator, thermostat) -> None:
        super().__init__(coordinator, thermostat, "climate")
        self._attr_name = climate_name(thermostat.name)
        self._fahrenheit = thermostat.temperature_scale == "F"
        self._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT if self._fahrenheit else UnitOfTemperature.CELSIUS
        self._attr_target_temperature_step = PRECISION_WHOLE if self._fahrenheit else PRECISION_HALVES
        self._attr_min_temp = self._native(_f_to_c(SCHEDULE_MIN_F))
        self._attr_max_temp = self._native(_f_to_c(SCHEDULE_MAX_F))
        self._attr_min_humidity, self._attr_max_humidity = HUMIDITY_MIN, HUMIDITY_MAX

    # --- units ---------------------------------------------------------------------

    def _native(self, celsius: float | None, digits: int = 0) -> float | None:
        if celsius is None:
            return None
        if self._fahrenheit:
            return round(celsius * 9 / 5 + 32, digits)
        return round(celsius * 2) / 2 if digits == 0 else round(celsius, digits)

    def _celsius(self, native: float) -> float:
        return (native - 32) * 5 / 9 if self._fahrenheit else native

    # --- state -----------------------------------------------------------------------

    @property
    def supported_features(self) -> ClimateEntityFeature:
        t = self.device
        f = (ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.PRESET_MODE
             | ClimateEntityFeature.TARGET_TEMPERATURE)
        if t.can_heat and t.can_cool:
            f |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if t.has_fan:
            f |= ClimateEntityFeature.FAN_MODE
        if t.has_humidifier or t.has_dehumidifier:
            f |= ClimateEntityFeature.TARGET_HUMIDITY
        return f

    @property
    def hvac_modes(self) -> list[HVACMode]:
        t = self.device
        modes = [HVACMode.OFF]
        if t.can_heat:
            modes.append(HVACMode.HEAT)
        if t.can_cool:
            modes.append(HVACMode.COOL)
        if t.can_heat and t.can_cool:
            modes.append(HVACMode.HEAT_COOL)
        return modes

    @property
    def hvac_mode(self) -> HVACMode | None:
        return _TO_HA.get(self.device.hvac_mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        return _ACTIONS.get(self.device.hvac_action)

    @property
    def current_temperature(self) -> float | None:
        return self._native(self.device.current_temperature, 1)

    @property
    def current_humidity(self) -> float | None:
        h = self.device.current_humidity
        return round(h) if h is not None else None

    def _targets(self) -> tuple[float | None, float | None]:
        """(heat, cool) the thermostat is holding to: the eco limits while in eco."""
        t = self.device
        if t.eco_active:
            heat_on, heat = t.threshold("eco_heat")
            cool_on, cool = t.threshold("eco_cool")
            return (heat if heat_on else None), (cool if cool_on else None)
        return t.target_heat, t.target_cool

    @property
    def target_temperature(self) -> float | None:
        heat, cool = self._targets()
        mode = self.device.hvac_mode
        return self._native(heat if mode == "heat" else cool if mode == "cool" else None)

    @property
    def target_temperature_low(self) -> float | None:
        return self._native(self._targets()[0]) if self.device.hvac_mode == "range" else None

    @property
    def target_temperature_high(self) -> float | None:
        return self._native(self._targets()[1]) if self.device.hvac_mode == "range" else None

    @property
    def preset_mode(self) -> str:
        return PRESET_ECO if self.device.eco_active else PRESET_NONE

    @property
    def fan_mode(self) -> str | None:
        return FAN_ON if self.device.fan_timer_end else FAN_AUTO

    @property
    def target_humidity(self) -> float | None:
        t = self.device
        return t.target_humidity if t.humidity_control_enabled else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self.device
        return {
            "active_sensor": _sensor_name(self, t.active_sensor),
            "eco_heat_limit": self._native(t.threshold("eco_heat")[1]),
            "eco_cool_limit": self._native(t.threshold("eco_cool")[1]),
            "fan_timer_end": t.fan_timer_end,
            "compressor_lockout_until": t.compressor_lockout_until,
            "schedule": self._schedule_attribute(),
            "sensor_schedule": self._sensor_schedule_attribute(),
        }

    def _sensor_schedule_attribute(self) -> dict[str, Any]:
        """The Home Assistant target sensor schedule, for the schedule card. Sensors
        are named by their temperature entity ("thermostat" for the thermostat's
        own), as the set_sensor_schedule action takes them."""
        registry = er.async_get(self.hass)
        entity = lambda domain, rid, key: registry.async_get_entity_id(domain, DOMAIN, f"{rid}-{key}")  # noqa: E731
        cache = self.coordinator.client.cache
        options = [{"id": THERMOSTAT_SENSOR, "name": "Thermostat",
                    "entity_id": entity("sensor", self.resource_id, "backplate_temperature")}]
        ids = {THERMOSTAT_SENSOR: THERMOSTAT_SENSOR}
        for sid in self.device.paired_sensors:
            if (eid := entity("sensor", sid, "temperature")) is not None:
                ids[sid] = eid
                options.append({"id": eid, "name": cache.display_name(sid), "entity_id": eid})
        scheduler = self.coordinator.sensor_schedule
        return {
            "enabled": scheduler.enabled(self.resource_id),
            "switch": entity("switch", self.resource_id, SENSOR_SCHEDULE_KEY),
            "options": options,
            "entries": [{**e.as_dict(), "sensors": [ids.get(x, x) for x in e.sensors]}
                        for e in scheduler.entries(self.resource_id)],
        }

    def _schedule_attribute(self) -> dict[str, Any]:
        """The weekly schedules in the thermostat's display units, for the
        schedule card: {"unit": "°F", "heat": [{"day": "mon", "time": "07:00",
        "temperature": 68}], "cool": [...], "range": [{..., "target_temp_low":
        66, "target_temp_high": 74}]}."""
        out: dict[str, Any] = {"unit": self.temperature_unit}
        for kind in SCHEDULE_LABELS:
            entries = []
            for day, seconds, heat, cool in self.device.schedule(kind):
                entry = {"day": day, "time": f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"}
                if kind == "range":
                    entry |= {"target_temp_low": self._native(heat), "target_temp_high": self._native(cool)}
                else:
                    entry["temperature"] = self._native(heat if kind == "heat" else cool)
                entries.append(entry)
            out[kind] = entries
        return out

    # --- actions ------------------------------------------------------------------------

    async def _leave_eco(self) -> None:
        if self.device.eco_active:
            await self.write("eco_mode_state", set_eco(False, user=self.user))

    @validation_errors
    async def async_set_temperature(self, **kwargs: Any) -> None:
        heat = cool = None
        # A bare temperature sets the setpoint of the mode being switched to, if any.
        mode = _FROM_HA[kwargs["hvac_mode"]] if kwargs.get("hvac_mode") is not None else self.device.hvac_mode
        if ATTR_TARGET_TEMP_LOW in kwargs or ATTR_TARGET_TEMP_HIGH in kwargs:
            if ATTR_TARGET_TEMP_LOW in kwargs:
                heat = self._celsius(kwargs[ATTR_TARGET_TEMP_LOW])
            if ATTR_TARGET_TEMP_HIGH in kwargs:
                cool = self._celsius(kwargs[ATTR_TARGET_TEMP_HIGH])
        elif ATTR_TEMPERATURE in kwargs:
            value = self._celsius(kwargs[ATTR_TEMPERATURE])
            if mode == "heat":
                heat = value
            else:
                cool = value
        if heat is None and cool is None:
            return
        mutations = [set_target_temperature(heat=heat, cool=cool, user=self.user)]
        if "hvac_mode" in kwargs and kwargs["hvac_mode"] is not None:
            mutations.insert(0, set_hvac_mode(_FROM_HA[kwargs["hvac_mode"]], user=self.user))
        await self._leave_eco()  # eco off first, or the new setpoint has no effect
        await self.write(TARGET, lambda trait: [m(trait) for m in mutations], check_target_temperature)

    @validation_errors
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        mode = _FROM_HA[hvac_mode]

        def mutate(trait) -> None:
            set_hvac_mode(mode, user=self.user)(trait)
            tt = trait.targetTemperature
            # Supply a setpoint the mode needs but the thermostat never stored
            # (cooling first: a range's default heating setpoint is derived from it).
            if mode in ("cool", "range") and not 4 <= tt.coolingTarget.value <= 38:
                tt.coolingTarget.value = _DEFAULT_COOL_C
            if mode in ("heat", "range") and not 4 <= tt.heatingTarget.value <= 38:
                tt.heatingTarget.value = min(_DEFAULT_HEAT_C, tt.coolingTarget.value - 1.7) if mode == "range" else _DEFAULT_HEAT_C

        await self.write(TARGET, mutate, check_target_temperature)

    @validation_errors
    async def async_turn_on(self) -> None:
        t = self.device
        await self.async_set_hvac_mode(HVACMode.HEAT_COOL if t.can_heat and t.can_cool else self.hvac_modes[-1])

    @validation_errors
    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    @validation_errors
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.write("eco_mode_state", set_eco(preset_mode == PRESET_ECO, user=self.user))

    @validation_errors
    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode == FAN_ON:
            duration = None if self.device.fan_run_duration else _DEFAULT_FAN_RUN
            await self.write("fan_control_settings", run_fan(duration))
        else:
            await self.write("fan_control_settings", stop_fan())

    @validation_errors
    async def async_set_humidity(self, humidity: int) -> None:
        value = int(round(humidity / HUMIDITY_STEP) * HUMIDITY_STEP)
        await self.write("humidity_control_settings", set_target_humidity(max(HUMIDITY_MIN, min(HUMIDITY_MAX, value))))


    # --- actions (services) --------------------------------------------------------------

    @validation_errors
    async def async_run_fan(self, duration: timedelta) -> None:
        await self.write("fan_control_settings", run_fan(int(duration.total_seconds())))

    def _sensor_resource(self, entity_id: str) -> str:
        """A ProtoNest remote sensor entity -> its resource ID."""
        entry = er.async_get(self.hass).async_get(entity_id)
        rid = entry.unique_id.split("-", 1)[0] if entry is not None and entry.platform == "protonest" else None
        # Not a thermostat's own sensor entities: only Nest Temperature Sensors pair.
        if rid is None or self.coordinator.client.cache.resource_types.get(rid) != SENSOR_TYPE:
            raise ServiceValidationError(f"{entity_id} is not a Nest Temperature Sensor")
        return rid

    @validation_errors
    async def async_average_sensors(self, sensors: list[str]) -> None:
        ids = list(dict.fromkeys(self._sensor_resource(e) for e in sensors))
        await self.write("remote_comfort_sensing_settings", set_multi_sensor(ids))

    @validation_errors
    async def async_pair_sensor(self, sensor: str) -> None:
        await self.write("remote_comfort_sensing_settings", pair_sensor(self._sensor_resource(sensor)))

    @validation_errors
    async def async_unpair_sensor(self, sensor: str) -> None:
        await self.write("remote_comfort_sensing_settings", unpair_sensor(self._sensor_resource(sensor)))

    def _schedule_temps(self, schedule: str, kwargs: dict) -> tuple[float | None, float | None]:
        """Temperatures in the entity's units -> (heat, cool) °C for a schedule kind."""
        conv = lambda key: self._celsius(kwargs[key]) if key in kwargs else None  # noqa: E731
        if schedule == "range":
            return conv("target_temp_low"), conv("target_temp_high")
        value = conv("temperature")
        return (value, None) if schedule == "heat" else (None, value)

    @validation_errors
    async def async_schedule_add(self, schedule: str, day: str, time: dtime, **kwargs: Any) -> None:
        heat, cool = self._schedule_temps(schedule, kwargs)
        await self.write(SCHEDULE_LABELS[schedule], add_schedule_entry(
            schedule, day, time.hour * 3600 + time.minute * 60, heat=heat, cool=cool, user=self.user))

    @validation_errors
    async def async_schedule_update(self, schedule: str, day: str, time: dtime, **kwargs: Any) -> None:
        heat, cool = self._schedule_temps(schedule, kwargs)
        new_time = kwargs.get("new_time")
        await self.write(SCHEDULE_LABELS[schedule], update_schedule_entry(
            schedule, day, time.hour * 3600 + time.minute * 60, new_day=kwargs.get("new_day"),
            new_seconds=new_time.hour * 3600 + new_time.minute * 60 if new_time else None,
            heat=heat, cool=cool, user=self.user))

    @validation_errors
    async def async_set_sensor_schedule(self, entries: list[dict[str, Any]]) -> None:
        resource = lambda s: s if s == THERMOSTAT_SENSOR else self._sensor_resource(s)  # noqa: E731
        schedule = [Entry(e["day"], e["time"].hour * 60 + e["time"].minute, tuple(resource(s) for s in e["sensors"]))
                    for e in entries]
        await self.coordinator.sensor_schedule.async_set_entries(
            self.resource_id, validate(schedule, self.device.paired_sensors))

    @validation_errors
    async def async_schedule_remove(self, schedule: str, day: str, time: dtime) -> None:
        await self.write(SCHEDULE_LABELS[schedule], remove_schedule_entry(
            schedule, day, time.hour * 3600 + time.minute * 60))


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def _sensor_name(entity: ProtoNestEntity, choice: str | None) -> str | None:
    if choice is None or not choice.startswith("DEVICE_"):
        return choice
    return entity.coordinator.client.cache.display_name(choice)
