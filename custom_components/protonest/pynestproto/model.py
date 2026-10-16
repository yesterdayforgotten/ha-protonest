"""Readable views of thermostats and remote sensors over the trait cache.

Values come from the newest state of each trait, ACCEPTED or CONFIRMED, so a
change shows up as soon as the server accepts it (~0.5 s) rather than when the
device confirms it (~1-2 s). Temperatures are °C; None means not reported.
"""

from __future__ import annotations

from datetime import UTC, datetime

from google.protobuf import message as pb_message

from .observe import TraitCache
from .proto_gen.nest.trait import hvac_pb2, occupancy_pb2
from .write import (
    DAYS,
    DISPLAY_FARSIGHT,
    HUMIDIFIER_TYPES,
    RCS,
    SCHEDULE_LABELS,
    SENSOR_PERIODS,
    SENSOR_SCHEDULE,
    SETTING_FLAGS,
    THERMOSTAT_SENSOR,
    THRESHOLDS,
)

THERMOSTAT_TYPES = ("nest.resource.NestLearningThermostat3Resource",)
STRUCTURE_TYPE = "nest.resource.structure.StructureResource"
SENSOR_TYPE = "nest.resource.NestKryptoniteResource"
SENSOR_AVERAGE = "average"  # active sensor choice when averaging

# Devices that report a product code instead of a name.
PRODUCT_NAMES = {"KR1": "Nest Temperature Sensor"}

SP = hvac_pb2.SetPointScheduleSettingsTrait
_MODES = {SP.SET_POINT_TYPE_HEAT: "heat", SP.SET_POINT_TYPE_COOL: "cool", SP.SET_POINT_TYPE_RANGE: "range"}
_FARSIGHT_NAMES = {v: k for k, v in DISPLAY_FARSIGHT.items()}
_HUMIDIFIER_NAMES = {v: k for k, v in HUMIDIFIER_TYPES.items()}
_DAY_NAMES = {v: k for k, v in DAYS.items()}


def _ts(timestamp) -> datetime | None:
    """A protobuf Timestamp as an aware datetime; None for unset/epoch."""
    if timestamp is None or timestamp.seconds <= 0:
        return None
    return timestamp.ToDatetime(UTC)


class Device:
    def __init__(self, cache: TraitCache, resource_id: str) -> None:
        self.cache = cache
        self.resource_id = resource_id

    def trait(self, label: str) -> pb_message.Message | None:
        value = self.cache.latest(self.resource_id, label)
        return value.message if value else None

    @property
    def name(self) -> str:
        return self.cache.display_name(self.resource_id)

    @property
    def serial_number(self) -> str | None:
        ident = self.trait("device_identity")
        return ident.serialNumber or None if ident else None

    @property
    def model_id(self) -> str | None:
        """The product description the device reports (a name, or a code like KR1)."""
        ident = self.trait("device_identity")
        return ident.productIdDescription.literal or None if ident else None

    @property
    def model(self) -> str | None:
        """A readable product name."""
        code = self.model_id
        return PRODUCT_NAMES.get(code, code)

    @property
    def firmware(self) -> str | None:
        ident = self.trait("device_identity")
        return ident.softwareVersion or None if ident else None

    @property
    def online(self) -> bool | None:
        live = self.trait("liveness")
        if live is None:
            return None
        return type(live).LivenessDeviceStatus.Name(live.status).endswith("ONLINE")

    def _temperature(self, label: str) -> float | None:
        t = self.trait(label)
        if t is None or not t.temperatureValue.HasField("temperature"):
            return None
        return t.temperatureValue.temperature.value


class RemoteSensor(Device):
    @property
    def temperature(self) -> float | None:
        return self._temperature("current_temperature")

    @property
    def battery_voltage(self) -> float | None:
        b = self.trait("battery")
        return b.assessedVoltage.value if b is not None and b.HasField("assessedVoltage") else None


class Thermostat(Device):
    # --- readings ---------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        """What the thermostat controls to: its own sensor, the active remote
        sensor, or the multi-sensor average."""
        return self._temperature("current_temperature")

    @property
    def backplate_temperature(self) -> float | None:
        return self._temperature("backplate_temperature")

    @property
    def current_humidity(self) -> float | None:
        h = self.trait("current_humidity")
        return h.humidityValue.humidity.value if h is not None and h.humidityValue.HasField("humidity") else None

    @property
    def battery_voltage(self) -> float | None:
        b = self.trait("battery_voltage")
        if b is None or not b.batteryValue.HasField("batteryVoltage"):
            return None
        return b.batteryValue.batteryVoltage.value

    @property
    def temperature_scale(self) -> str:
        d = self.trait("display_settings")
        return "C" if d is not None and d.temperatureScale == hvac_pb2.DisplaySettingsTrait.TEMPERATURE_SCALE_C else "F"

    # --- capabilities -------------------------------------------------------------

    def _capability(self, name: str) -> bool:
        caps = self.trait("hvac_equipment_capabilities")
        return bool(caps is not None and getattr(caps, name))

    @property
    def can_heat(self) -> bool:
        return any(self._capability(n) for n in ("hasStage1Heat", "hasStage1AlternateHeat", "hasAuxHeat", "hasEmergencyHeat"))

    @property
    def can_cool(self) -> bool:
        return self._capability("hasStage1Cool")

    @property
    def has_humidifier(self) -> bool:
        return self._capability("hasHumidifier")

    @property
    def has_dehumidifier(self) -> bool:
        return self._capability("hasDehumidifier")

    @property
    def has_fan(self) -> bool:
        return self.trait("fan_control_settings") is not None

    @property
    def fan_speeds(self) -> int:
        caps = self.trait("fan_control_capabilities")
        if caps is None:
            return 1
        name = hvac_pb2.FanControlTrait.FanSpeedSetting.Name(caps.maxAvailableSpeed)
        return int(name[-1]) if name[-1].isdigit() else 1

    # --- mode and setpoints ---------------------------------------------------------

    @property
    def hvac_mode(self) -> str | None:
        """'off', 'heat', 'cool' or 'range'."""
        t = self.trait("target_temperature_settings")
        if t is None:
            return None
        if not t.enabled.value:
            return "off"
        return _MODES.get(t.targetTemperature.setpointType)

    @property
    def target_heat(self) -> float | None:
        t = self.trait("target_temperature_settings")
        return t.targetTemperature.heatingTarget.value if t is not None else None

    @property
    def target_cool(self) -> float | None:
        t = self.trait("target_temperature_settings")
        return t.targetTemperature.coolingTarget.value if t is not None else None

    @property
    def hvac_action(self) -> str | None:
        """'heating', 'cooling', 'fan', 'idle', or 'off'."""
        if self.hvac_mode == "off" and not self.fan_running:
            return "off"
        c = self.trait("hvac_control")
        if c is None:
            return None
        s = c.hvacState
        if any((s.heatStage1Active, s.heatStage2Active, s.heatStage3Active, s.alternateHeatStage1Active,
                s.alternateHeatStage2Active, s.auxiliaryHeatActive, s.emergencyHeatActive)):
            return "heating"
        if any((s.coolStage1Active, s.coolStage2Active, s.coolStage3Active)):
            return "cooling"
        if self.fan_running:
            return "fan"
        return "idle"

    @property
    def active_stages(self) -> list[str]:
        c = self.trait("hvac_control")
        return [f.name for f, v in c.hvacState.ListFields() if v] if c is not None else []

    @property
    def compressor_lockout_until(self) -> datetime | None:
        c = self.trait("hvac_control")
        until = _ts(c.compressorLockoutTimeout) if c is not None else None
        return until if until and until > datetime.now(UTC) else None

    @property
    def time_to_target(self) -> int | None:
        t = self.trait("time_to_temperature")
        return t.timeToTargetTemperature.seconds if t is not None and t.HasField("timeToTargetTemperature") else None

    # --- eco ------------------------------------------------------------------------------

    @property
    def eco_active(self) -> bool:
        e = self.trait("eco_mode_state")
        return e is not None and e.ecoMode in (
            hvac_pb2.EcoModeStateTrait.ECO_MODE_MANUAL_ECO, hvac_pb2.EcoModeStateTrait.ECO_MODE_AUTO_ECO)

    @property
    def leaf(self) -> bool | None:
        l = self.trait("leaf")
        return l.active if l is not None else None

    def threshold(self, name: str) -> tuple[bool, float | None]:
        """(enabled, °C) for an eco or safety limit in THRESHOLDS."""
        spec = THRESHOLDS[name]
        t = self.trait(spec.label)
        if t is None:
            return False, None
        th = getattr(t, spec.field)
        return th.enabled, th.value.value if th.HasField("value") else None

    # --- humidity ---------------------------------------------------------------------------

    @property
    def target_humidity(self) -> float | None:
        h = self.trait("humidity_control_settings")
        return h.targetHumidity.value if h is not None else None

    @property
    def humidity_control_enabled(self) -> bool:
        h = self.trait("humidity_control_settings")
        return bool(h is not None and h.targetHumidity.enabled)

    @property
    def humidifier_type(self) -> str | None:
        e = self.trait("equipment_settings")
        return _HUMIDIFIER_NAMES.get(e.humidifierType) if e is not None else None

    @property
    def humidifier_runs_fan(self) -> bool | None:
        e = self.trait("equipment_settings")
        if e is None or not e.humidifierFanActivation:
            return None
        return e.humidifierFanActivation == hvac_pb2.EquipmentSettingsTrait.HUMIDIFIER_FAN_TYPE_ACTIVATE

    # --- fan ---------------------------------------------------------------------------------

    @property
    def fan_running(self) -> bool:
        f = self.trait("fan_control")
        return f is not None and f.currentSpeed not in (
            hvac_pb2.FanControlTrait.FAN_SPEED_SETTING_OFF, hvac_pb2.FanControlTrait.FAN_SPEED_SETTING_UNSPECIFIED)

    @property
    def fan_timer_end(self) -> datetime | None:
        f = self.trait("fan_control_settings")
        end = _ts(f.timerEnd) if f is not None else None
        return end if end and end > datetime.now(UTC) else None

    @property
    def fan_run_duration(self) -> int | None:
        f = self.trait("fan_control_settings")
        return f.timerDuration.seconds if f is not None and f.HasField("timerDuration") else None

    @property
    def fan_schedule_enabled(self) -> bool:
        f = self.trait("fan_control_settings")
        return f is not None and f.mode == hvac_pb2.FanControlSettingsTrait.FAN_MODE_DUTY_CYCLE

    @property
    def fan_schedule_minutes(self) -> int | None:
        f = self.trait("fan_control_settings")
        return f.scheduleDutyCycle // 60 if f is not None and f.scheduleDutyCycle else None

    @property
    def fan_schedule_hours(self) -> tuple[int, int] | None:
        f = self.trait("fan_control_settings")
        return (f.scheduleStartTime // 3600, f.scheduleEndTime // 3600) if f is not None else None

    # --- target sensor -------------------------------------------------------------------------

    def _rcs(self) -> hvac_pb2.RemoteComfortSensingSettingsTrait | None:
        return self.trait("remote_comfort_sensing_settings")

    @property
    def paired_sensors(self) -> list[str]:
        r = self._rcs()
        return [s.deviceId.resourceId for s in r.associatedRcsSensors] if r is not None else []

    @property
    def active_sensor(self) -> str | None:
        """THERMOSTAT_SENSOR, SENSOR_SCHEDULE, SENSOR_AVERAGE, or a sensor's resource ID."""
        r = self._rcs()
        if r is None:
            return None
        if r.rcsControlMode == RCS.RCS_CONTROL_MODE_SCHEDULE:
            return SENSOR_SCHEDULE
        sel = r.activeRcsSelection
        if sel.rcsSourceType == RCS.RCS_SOURCE_TYPE_MULTI_SENSOR:
            return SENSOR_AVERAGE
        if sel.rcsSourceType == RCS.RCS_SOURCE_TYPE_SINGLE_SENSOR and sel.activeRcsSensor.resourceId:
            return sel.activeRcsSensor.resourceId
        return THERMOSTAT_SENSOR

    @property
    def average_sensors(self) -> list[str]:
        r = self._rcs()
        if r is None or not r.multiSensorSettings.multiSensorEnabled:
            return []
        return [s.resourceId for s in r.multiSensorSettings.multiSensorGroup]

    @property
    def has_sensor_schedule(self) -> bool:
        r = self._rcs()
        return r is not None and sorted(r.rcsControlSchedule.intervals) == [0, 1, 2, 3]

    def sensor_for_period(self, period: str) -> str | None:
        r = self._rcs()
        if r is None or not self.has_sensor_schedule:
            return None
        sel = r.rcsControlSchedule.intervals[SENSOR_PERIODS.index(period)].rcsSelection
        if sel.rcsSourceType == RCS.RCS_SOURCE_TYPE_SINGLE_SENSOR and sel.activeRcsSensor.resourceId:
            return sel.activeRcsSensor.resourceId
        return THERMOSTAT_SENSOR

    # --- settings -----------------------------------------------------------------------------

    def flag(self, name: str) -> bool | None:
        label, field = SETTING_FLAGS[name]
        t = self.trait(label)
        return getattr(t, field) if t is not None else None

    @property
    def early_on_hours(self) -> int | None:
        """None when Early-On is off."""
        p = self.trait("preconditioning_settings")
        if p is None or not p.preconditioningEnabled:
            return None
        return p.maxNighttimeSeconds.seconds // 3600

    @property
    def farsight(self) -> str | None:
        d = self.trait("display_settings")
        return _FARSIGHT_NAMES.get(d.farsightDisplay) if d is not None else None

    # --- filter ---------------------------------------------------------------------------------

    @property
    def filter_changed(self) -> datetime | None:
        """None when never set or set to the app's "more than 6 months ago"."""
        f = self.trait("filter_reminder_settings")
        if f is None or f.filterChangedDate.seconds <= 1:
            return None
        return f.filterChangedDate.ToDatetime(UTC)

    @property
    def filter_threshold_hours(self) -> float | None:
        f = self.trait("filter_reminder_settings")
        return f.filterReplacementThreshold.seconds / 3600 if f is not None else None

    @property
    def filter_runtime_hours(self) -> float | None:
        f = self.trait("filter_reminder")
        return f.filterRuntime.seconds / 3600 if f is not None else None

    @property
    def filter_replacement_needed(self) -> bool | None:
        f = self.trait("filter_reminder")
        return f.filterReplacementNeeded.value if f is not None and f.HasField("filterReplacementNeeded") else None

    @property
    def filter_reminder_level(self) -> str | None:
        f = self.trait("filter_reminder")
        if f is None:
            return None
        return hvac_pb2.FilterReminderTrait.FilterReminderLevel.Name(f.currentFilterReminderLevel).removeprefix(
            "FILTER_REMINDER_LEVEL_").lower()

    @property
    def wifi_signal(self) -> int | None:
        """Wi-Fi signal strength in dBm (reported as a positive number)."""
        w = self.trait("wifi_interface")
        return -abs(w.rssi) if w is not None and w.rssi else None

    @property
    def seasonal_savings(self) -> str | None:
        """Where the thermostat is in a Seasonal Savings offer; None if it never had one."""
        s = self.trait("seasonal_savings")
        if s is None or not s.state:
            return None
        return _SAVINGS_STATES.get(s.state, "unknown")

    # --- weekly schedules -------------------------------------------------------------------------

    def schedule(self, kind: str) -> list[tuple[str, int, float | None, float | None]]:
        """[(day, seconds_in_day, heat_c, cool_c)] Monday-first, None for unused targets."""
        t = self.trait(SCHEDULE_LABELS[kind])
        if t is None:
            return []
        order = list(DAYS.values())
        out = []
        for sp in sorted(t.setpoints.values(), key=lambda s: (order.index(s.dayOfWeek) if s.dayOfWeek in order else 9, s.secondsInDay)):
            heat = sp.heatingTarget.value if kind in ("heat", "range") else None
            cool = sp.coolingTarget.value if kind in ("cool", "range") else None
            out.append((_DAY_NAMES.get(sp.dayOfWeek, "?"), sp.secondsInDay, heat, cool))
        return out


def thermostats(cache: TraitCache) -> list[Thermostat]:
    return [Thermostat(cache, rid) for rid, t in cache.resource_types.items() if t in THERMOSTAT_TYPES]


def remote_sensors(cache: TraitCache) -> list[RemoteSensor]:
    return [RemoteSensor(cache, rid) for rid, t in cache.resource_types.items() if t == SENSOR_TYPE]


def structures(cache: TraitCache) -> list[Structure]:
    return [Structure(cache, rid) for rid, t in cache.resource_types.items() if t == STRUCTURE_TYPE]


_SM = occupancy_pb2.StructureModeTrait
STRUCTURE_MODES = {_SM.STRUCTURE_MODE_HOME: "home", _SM.STRUCTURE_MODE_AWAY: "away",
                   _SM.STRUCTURE_MODE_SLEEP: "sleep", _SM.STRUCTURE_MODE_VACATION: "vacation"}
_SS = hvac_pb2.SeasonalSavingsTrait
_SAVINGS_STATES = {
    _SS.SEASONAL_SAVINGS_STATE_INITIAL: "waiting_to_qualify",
    _SS.SEASONAL_SAVINGS_STATE_WAITING_TO_QUALIFY: "waiting_to_qualify",
    _SS.SEASONAL_SAVINGS_STATE_PRESENTING_EVENT: "offered",
    _SS.SEASONAL_SAVINGS_STATE_WAITING: "starting",
    _SS.SEASONAL_SAVINGS_STATE_RUNNING: "running",
    _SS.SEASONAL_SAVINGS_STATE_PAUSED: "paused",
    _SS.SEASONAL_SAVINGS_STATE_PRESENTING_FINAL_STAGE_COMPLETED: "completed",
    _SS.SEASONAL_SAVINGS_STATE_AFTERGLOW: "completed",
    _SS.SEASONAL_SAVINGS_STATE_FINISHED: "finished",
}
SAVINGS_STATES = sorted(set(_SAVINGS_STATES.values())) + ["unknown"]


class Structure(Device):
    """The Nest home: Home/Away and HVAC alerts."""

    @property
    def name(self) -> str:
        info = self.trait("structure_info")
        return f"Nest {info.name}" if info is not None and info.name else "Nest home"

    @property
    def mode(self) -> str | None:
        m = self.trait("structure_mode")
        return STRUCTURE_MODES.get(m.structureMode) if m is not None else None

    @property
    def hvac_alerts(self) -> bool | None:
        a = self.trait("hvac_diagnostics_alerts_settings")
        return a.enableAlerts.value if a is not None and a.HasField("enableAlerts") else None

    @property
    def last_hvac_alert(self) -> datetime | None:
        a = self.trait("hvac_diagnostics_alerts_settings")
        return _ts(a.lastAlertTime) if a is not None and a.HasField("lastAlertTime") else None
