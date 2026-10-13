"""Writing trait state with BatchUpdateState.

The server replaces the whole trait with what we send (the web app sends no
field mask), so every write is read-modify-write: copy the newest cached
value, change only what's intended, and send the complete trait back. Writing
without a cached value would blank every field we didn't set, so it's refused.

Mutation helpers for specific traits live here too. They change a message in
place and, if anything actually changed, stamp ``currentActorInfo`` the way the
Nest apps do, so changes are attributed to the user in the Nest app's history.
Setting a value it already has is a no-op, and NestWriter sends nothing.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from google.protobuf import message as pb_message

from . import codec
from .observe import TraitCache
from .proto_gen.nest.trait import hvac_pb2
from .proto_gen.nestlabs.gateway import v1_pb2
from .transport import BATCH_UPDATE_STATE_PATH, NestTransport

SP = hvac_pb2.SetPointScheduleSettingsTrait
ACTOR_WEB = hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_WEB

# A sanity guard against unit mix-ups (e.g. 75 passed as Celsius), not the
# device's real limits, which the HA layer enforces from capabilities.
_SANE_C = (4.0, 38.0)

HVAC_MODES = ("off", "heat", "cool", "range")
_SETPOINT_TYPES = {
    "heat": SP.SET_POINT_TYPE_HEAT,
    "cool": SP.SET_POINT_TYPE_COOL,
    "range": SP.SET_POINT_TYPE_RANGE,
}


class WriteError(Exception):
    """A write was refused locally or rejected by the server."""


@dataclass(frozen=True)
class PreparedUpdate:
    resource_id: str
    label: str
    old: pb_message.Message
    new: pb_message.Message
    request: v1_pb2.BatchUpdateStateRequest

    @property
    def changed(self) -> bool:
        return self.old != self.new


def build_request(
    resource_id: str, label: str, trait: pb_message.Message, request_id: str | None = None
) -> v1_pb2.BatchUpdateStateRequest:
    update = v1_pb2.TraitUpdateStateRequest(state=codec.pack(trait))
    update.traitRequest.resourceId = resource_id
    update.traitRequest.traitLabel = label
    update.traitRequest.requestId = request_id or str(uuid.uuid4())
    return v1_pb2.BatchUpdateStateRequest(batchUpdateStateRequest=[update])


def prepare_update(
    cache: TraitCache,
    resource_id: str,
    label: str,
    mutate: Callable[[pb_message.Message], None],
    request_id: str | None = None,
    base: pb_message.Message | None = None,
) -> PreparedUpdate:
    """Copy the newest cached trait (or ``base``), apply ``mutate``, and build the request.

    ``base`` is for a value sent moments ago that the stream hasn't echoed yet.
    """
    if base is None:
        current = cache.latest(resource_id, label)
        if current is None or current.message is None:
            raise WriteError(
                f"no cached {label} for {resource_id}; refusing to write without the current state"
            )
        base = current.message
    new = type(base)()
    new.CopyFrom(base)
    mutate(new)
    return PreparedUpdate(resource_id, label, base, new, build_request(resource_id, label, new, request_id))


class NestWriter:
    """Sends trait updates built from the Observe cache."""

    def __init__(self, transport: NestTransport, cache: TraitCache) -> None:
        self._transport = transport
        self._cache = cache

    async def async_send(self, prepared: PreparedUpdate) -> v1_pb2.TraitOperation | None:
        """Send a prepared update. Returns None (and sends nothing) if it's a no-op."""
        if not prepared.changed:
            return None
        frames = [
            codec.UpdateStateFrame.FromString(m)
            for m in await self._transport.async_call(BATCH_UPDATE_STATE_PATH, prepared.request)
        ]
        ops = [op for f in frames for op in f.traitOperations]
        if not ops:
            raise WriteError(f"{prepared.label}: server returned no operation result")
        op = ops[0]
        if op.status.code != 0:
            raise WriteError(f"{prepared.label}: rejected ({op.status.code}: {op.status.message})")
        return op

    async def async_update(
        self, resource_id: str, label: str, mutate: Callable[[pb_message.Message], None]
    ) -> v1_pb2.TraitOperation | None:
        return await self.async_send(prepare_update(self._cache, resource_id, label, mutate))


# --- Mutations ----------------------------------------------------------------


def stamp_actor(
    actor: hvac_pb2.HvacActor.HvacActorStruct,
    user_resource_id: str | None,
    when: datetime | None = None,
    method: int = ACTOR_WEB,
) -> None:
    """Record who made a change and when (whole seconds, like the web app)."""
    actor.method = method
    if user_resource_id:
        actor.originator.resourceId = user_resource_id
    actor.timeOfAction.FromDatetime((when or datetime.now(UTC)).replace(microsecond=0))


def _check_celsius(name: str, value: float) -> float:
    if not math.isfinite(value) or not _SANE_C[0] <= value <= _SANE_C[1]:
        raise ValueError(
            f"{name}={value} is outside {_SANE_C[0]}-{_SANE_C[1]} °C; temperatures are in Celsius"
        )
    return value


def set_target_temperature(
    *,
    heat: float | None = None,
    cool: float | None = None,
    user: str | None = None,
    when: datetime | None = None,
    method: int = ACTOR_WEB,
) -> Callable[[hvac_pb2.TargetTemperatureSettingsTrait], None]:
    """Mutation: change the heating and/or cooling setpoint (°C)."""
    if heat is None and cool is None:
        raise ValueError("give heat and/or cool")
    if heat is not None:
        _check_celsius("heat", heat)
    if cool is not None:
        _check_celsius("cool", cool)

    def mutate(trait: hvac_pb2.TargetTemperatureSettingsTrait) -> None:
        tt = trait.targetTemperature
        before = (tt.heatingTarget.value, tt.coolingTarget.value)
        if heat is not None:
            tt.heatingTarget.value = heat
        if cool is not None:
            tt.coolingTarget.value = cool
        if (tt.heatingTarget.value, tt.coolingTarget.value) != before:
            stamp_actor(tt.currentActorInfo, user, when, method)

    return mutate


def set_hvac_mode(
    mode: str,
    *,
    user: str | None = None,
    when: datetime | None = None,
    method: int = ACTOR_WEB,
) -> Callable[[hvac_pb2.TargetTemperatureSettingsTrait], None]:
    """Mutation: switch mode. "off" only clears ``enabled``, as the app does,
    so the previous heat/cool/range mode is kept for when it's turned back on."""
    if mode not in HVAC_MODES:
        raise ValueError(f"mode must be one of {HVAC_MODES}")

    def mutate(trait: hvac_pb2.TargetTemperatureSettingsTrait) -> None:
        before = (trait.enabled.value, trait.targetTemperature.setpointType)
        if mode == "off":
            trait.enabled.value = False
        else:
            trait.enabled.value = True
            trait.targetTemperature.setpointType = _SETPOINT_TYPES[mode]
        if (trait.enabled.value, trait.targetTemperature.setpointType) != before:
            stamp_actor(trait.targetTemperature.currentActorInfo, user, when, method)

    return mutate


def check_target_temperature(trait: hvac_pb2.TargetTemperatureSettingsTrait) -> None:
    """Refuse a target-temperature state that would make no sense to send.

    A thermostat only ever used for cooling has no heating setpoint stored
    (it reads 0.0), so switching it to heat or range without also giving a
    heating setpoint would send 0 °C. Raises ValueError explaining what's missing.
    """
    if not trait.enabled.value:
        return  # off: setpoints are kept but unused
    tt = trait.targetTemperature
    kind = tt.setpointType
    needs_heat = kind in (SP.SET_POINT_TYPE_HEAT, SP.SET_POINT_TYPE_RANGE)
    needs_cool = kind in (SP.SET_POINT_TYPE_COOL, SP.SET_POINT_TYPE_RANGE)
    lo, hi = _SANE_C
    if needs_heat and not lo <= tt.heatingTarget.value <= hi:
        raise ValueError(f"no valid heating setpoint ({tt.heatingTarget.value:.1f} °C); give one with this mode")
    if needs_cool and not lo <= tt.coolingTarget.value <= hi:
        raise ValueError(f"no valid cooling setpoint ({tt.coolingTarget.value:.1f} °C); give one with this mode")
    if kind == SP.SET_POINT_TYPE_RANGE and tt.heatingTarget.value >= tt.coolingTarget.value:
        raise ValueError("in range mode the heating setpoint must be below the cooling setpoint")


# --- Fan ------------------------------------------------------------------------
#
# All in fan_control_settings, which has no actor field. Values are the ones the
# Nest app offers (observed 2026-10-04); the thermostat confirmed each of them.

FAN = hvac_pb2.FanControlSettingsTrait

# Periodic schedule: seconds of fan per hour (app: 15, 30, 45, 60 min).
FAN_RUN_TIMES = (900, 1800, 2700, 3600)

# Manual "run fan for" lengths (app: 15m 30m 45m 1h 2h 4h 8h 12h 1d 3d 5d 1w).
FAN_TIMER_DURATIONS = (
    900, 1800, 2700, 3600, 7200, 14400, 28800, 43200, 86400, 259200, 432000, 604800,
)
_MAX_FAN_TIMER = 604800  # the thermostats' maxTimerDuration; no indefinite timer


def set_fan_schedule(
    *,
    enabled: bool | None = None,
    run_seconds_per_hour: int | None = None,
    start_hour: int | None = None,
    end_hour: int | None = None,
) -> Callable[[hvac_pb2.FanControlSettingsTrait], None]:
    """Mutation: the periodic fan schedule.

    Hours are 0-23 (the app only offers whole hours). End before start wraps
    past midnight; start == end means all day.
    """
    if run_seconds_per_hour is not None and run_seconds_per_hour not in FAN_RUN_TIMES:
        raise ValueError(f"run time must be one of {FAN_RUN_TIMES} seconds per hour")
    for name, hour in (("start_hour", start_hour), ("end_hour", end_hour)):
        if hour is not None and not (isinstance(hour, int) and 0 <= hour <= 23):
            raise ValueError(f"{name} must be a whole hour 0-23")

    def mutate(trait: hvac_pb2.FanControlSettingsTrait) -> None:
        if enabled is not None:
            trait.mode = FAN.FAN_MODE_DUTY_CYCLE if enabled else FAN.FAN_MODE_AUTO
        if run_seconds_per_hour is not None:
            trait.scheduleDutyCycle = run_seconds_per_hour
        if start_hour is not None:
            trait.scheduleStartTime = start_hour * 3600
        if end_hour is not None:
            trait.scheduleEndTime = end_hour * 3600

    return mutate


def run_fan(
    duration_s: int | None = None, *, now: datetime | None = None
) -> Callable[[hvac_pb2.FanControlSettingsTrait], None]:
    """Mutation: run the fan now for ``duration_s`` (default: the thermostat's
    remembered length). Like the app, also remembers the length for next time."""
    if duration_s is not None and not 60 <= duration_s <= _MAX_FAN_TIMER:
        raise ValueError(f"fan run duration must be 60-{_MAX_FAN_TIMER} seconds")

    def mutate(trait: hvac_pb2.FanControlSettingsTrait) -> None:
        seconds = duration_s if duration_s is not None else trait.timerDuration.seconds
        if seconds <= 0:
            raise ValueError("no remembered fan run duration; give one")
        start = (now or datetime.now(UTC)).replace(microsecond=0)
        trait.timerDuration.FromSeconds(seconds)
        trait.timerEnd.FromSeconds(int(start.timestamp()) + seconds)

    return mutate


def stop_fan() -> Callable[[hvac_pb2.FanControlSettingsTrait], None]:
    """Mutation: cancel a manual fan run. The schedule is unaffected."""

    def mutate(trait: hvac_pb2.FanControlSettingsTrait) -> None:
        # The server's form for "no timer": present, at the epoch.
        trait.timerEnd.Clear()
        trait.timerEnd.SetInParent()

    return mutate


def set_fan_run_duration(duration_s: int) -> Callable[[hvac_pb2.FanControlSettingsTrait], None]:
    """Mutation: change the remembered manual run length without starting the fan."""
    if duration_s not in FAN_TIMER_DURATIONS:
        raise ValueError(f"duration must be one of {FAN_TIMER_DURATIONS} seconds")

    def mutate(trait: hvac_pb2.FanControlSettingsTrait) -> None:
        trait.timerDuration.FromSeconds(duration_s)

    return mutate



# --- Humidity -------------------------------------------------------------------

# The Nest app's range and step (observed 2026-10-04). Only meaningful on a
# thermostat with a humidifier or dehumidifier (HvacEquipmentCapabilitiesTrait).
HUMIDITY_MIN, HUMIDITY_MAX, HUMIDITY_STEP = 10, 60, 5


def set_target_humidity(percent: int) -> Callable[[hvac_pb2.HumidityControlSettingsTrait], None]:
    """Mutation: target humidity. Only the value changes, as in the app."""
    if (
        not isinstance(percent, int)
        or not HUMIDITY_MIN <= percent <= HUMIDITY_MAX
        or percent % HUMIDITY_STEP
    ):
        raise ValueError(
            f"humidity must be {HUMIDITY_MIN}-{HUMIDITY_MAX}% in steps of {HUMIDITY_STEP}"
        )

    def mutate(trait: hvac_pb2.HumidityControlSettingsTrait) -> None:
        trait.targetHumidity.value = percent

    return mutate


# --- Target (remote comfort) sensor --------------------------------------------

RCS = hvac_pb2.RemoteComfortSensingSettingsTrait
THERMOSTAT_SENSOR = "thermostat"  # the thermostat's own (backplate) sensor
SENSOR_SCHEDULE = "schedule"  # follow the sensor schedule


def select_sensor(choice: str) -> Callable[[hvac_pb2.RemoteComfortSensingSettingsTrait], None]:
    """Mutation: which sensor the thermostat controls to.

    ``choice`` is THERMOSTAT_SENSOR, SENSOR_SCHEDULE, or a paired sensor's
    resource ID. Matches the app: a pick holds (mode HOLD) and replaces the
    active selection; "schedule" only switches the mode, and the thermostat
    itself then selects the sensor for the current period.
    """

    def mutate(trait: hvac_pb2.RemoteComfortSensingSettingsTrait) -> None:
        if choice == SENSOR_SCHEDULE:
            trait.rcsControlMode = RCS.RCS_CONTROL_MODE_SCHEDULE
            return
        selection = trait.activeRcsSelection
        if choice == THERMOSTAT_SENSOR:
            selection.rcsSourceType = RCS.RCS_SOURCE_TYPE_BACKPLATE
            selection.ClearField("activeRcsSensor")
        else:
            paired = {s.deviceId.resourceId for s in trait.associatedRcsSensors}
            if choice not in paired:
                raise ValueError(f"{choice} is not paired with this thermostat")
            selection.rcsSourceType = RCS.RCS_SOURCE_TYPE_SINGLE_SENSOR
            selection.activeRcsSensor.resourceId = choice
        trait.rcsControlMode = RCS.RCS_CONTROL_MODE_HOLD

    return mutate


# --- Eco ------------------------------------------------------------------------

ECO = hvac_pb2.EcoModeStateTrait


def set_eco(
    on: bool,
    *,
    user: str | None = None,
    when: datetime | None = None,
    method: int = ACTOR_WEB,
) -> Callable[[hvac_pb2.EcoModeStateTrait], None]:
    """Mutation: manual eco on/off, recorded as a manual change by the user."""

    def mutate(trait: hvac_pb2.EcoModeStateTrait) -> None:
        target = ECO.ECO_MODE_MANUAL_ECO if on else ECO.ECO_MODE_INACTIVE
        if trait.ecoMode == target:
            return
        trait.ecoMode = target
        trait.ecoModeChangeReason = ECO.ECO_MODE_CHANGE_REASON_MANUAL
        stamp_actor(trait.ecoModeActor, user, when, method)

    return mutate


# --- Thermostat settings ----------------------------------------------------------
#
# Observed from the Nest app (2026-10-04). Every one of these is a
# plain field in its own settings trait.

# On/off features: (trait label, field). Keys are this package's names.
SETTING_FLAGS: dict[str, tuple[str, str]] = {
    "auto_schedule": ("schedule_learning_settings", "scheduleLearningEnabled"),
    "home_away_assist": ("eco_mode_settings", "structureModeFollowEnabled"),
    "cool_to_dry": ("cool_to_dry_settings", "enabled"),
    "sunblock": ("sunblock_settings", "enabled"),
    "airwave": ("airwave_settings", "fanCoolingEnabled"),
    "wake_on_approach": ("wake_on_approach_settings", "enabled"),
    "filter_reminder": ("filter_reminder_settings", "filterReminderEnabled"),
}


def set_flag(name: str, on: bool) -> Callable[[pb_message.Message], None]:
    """Mutation: switch one of SETTING_FLAGS on or off (use its trait label)."""
    _, field = SETTING_FLAGS[name]

    def mutate(trait: pb_message.Message) -> None:
        setattr(trait, field, on)

    return mutate


# Early-On: off, or on with up to 0-5 h of nighttime pre-conditioning.
EARLY_ON_LABEL = "preconditioning_settings"
EARLY_ON_HOURS = range(0, 6)


def set_early_on(hours: int | None) -> Callable[[hvac_pb2.PreconditioningSettingsTrait], None]:
    """Mutation: None turns Early-On off (keeping the hours, as the app does)."""
    if hours is not None and hours not in EARLY_ON_HOURS:
        raise ValueError("Early-On hours must be 0-5")

    def mutate(trait: hvac_pb2.PreconditioningSettingsTrait) -> None:
        trait.preconditioningEnabled = hours is not None
        if hours is not None:
            trait.maxNighttimeSeconds.FromSeconds(hours * 3600)

    return mutate


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


@dataclass(frozen=True)
class ThresholdSpec:
    """A {value, enabled} temperature limit. Off parks the value at off_c."""

    label: str
    field: str
    min_f: float  # enabled range, °F (the app's slider, minus its "off" end)
    max_f: float
    off_c: float  # exactly what the app writes when switching it off (°C, float32)

    def check(self, celsius: float) -> None:
        lo, hi = _f_to_c(self.min_f), _f_to_c(self.max_f)
        if not math.isfinite(celsius) or not lo - 0.01 <= celsius <= hi + 0.01:
            raise ValueError(
                f"{self.field} must be {self.min_f:g}-{self.max_f:g}°F "
                f"({lo:.1f}-{hi:.1f}°C), or off"
            )


THRESHOLDS: dict[str, ThresholdSpec] = {
    # Eco: "off" sits below the heat range and above the cool range.
    "eco_heat": ThresholdSpec("eco_mode_settings", "ecoTemperatureHeat", 40.5, 70, off_c=4.4444447),  # 40°F
    "eco_cool": ThresholdSpec("eco_mode_settings", "ecoTemperatureCool", 76, 89.5, off_c=32.222225),  # 90°F
    # Safety: protects the house (e.g. pipes) even when the system is off.
    "safety_low": ThresholdSpec("safety_temperature_settings", "lowerSafetyTemp", 35.5, 45, off_c=1.6666667),  # 35°F
    "safety_high": ThresholdSpec("safety_temperature_settings", "upperSafetyTemp", 95, 104.5, off_c=40.555557),  # 105°F
}


def set_threshold(name: str, celsius: float | None) -> Callable[[pb_message.Message], None]:
    """Mutation: set one of THRESHOLDS to a temperature (°C), or None for off."""
    spec = THRESHOLDS[name]
    if celsius is not None:
        spec.check(celsius)

    def mutate(trait: pb_message.Message) -> None:
        threshold = getattr(trait, spec.field)
        if celsius is None:
            if threshold.enabled or not threshold.HasField("value"):
                threshold.enabled = False
                threshold.value.value = spec.off_c
        else:
            threshold.enabled = True
            threshold.value.value = celsius

    return mutate


DISPLAY_LABEL = "display_settings"
_SCALES = {
    "C": hvac_pb2.DisplaySettingsTrait.TEMPERATURE_SCALE_C,
    "F": hvac_pb2.DisplaySettingsTrait.TEMPERATURE_SCALE_F,
}


def set_temperature_scale(unit: str) -> Callable[[hvac_pb2.DisplaySettingsTrait], None]:
    """Mutation: the units the thermostat's own display uses ("C" or "F")."""
    if unit not in _SCALES:
        raise ValueError('unit must be "C" or "F"')

    def mutate(trait: hvac_pb2.DisplaySettingsTrait) -> None:
        trait.temperatureScale = _SCALES[unit]

    return mutate


# --- Sensor pairing -------------------------------------------------------------
#
# A remote sensor can be paired with several thermostats at once; pairing is
# the thermostat's associatedRcsSensors list.

KR1_VENDOR_ID, KR1_PRODUCT_ID = 9050, 26  # Nest, Temperature Sensor (as listed by the app)


def pair_sensor(
    sensor_id: str, vendor_id: int = KR1_VENDOR_ID, product_id: int = KR1_PRODUCT_ID
) -> Callable[[hvac_pb2.RemoteComfortSensingSettingsTrait], None]:
    """Mutation: add a sensor to the thermostat's paired sensors (appended, as the app does)."""

    def mutate(trait: hvac_pb2.RemoteComfortSensingSettingsTrait) -> None:
        if any(s.deviceId.resourceId == sensor_id for s in trait.associatedRcsSensors):
            return
        entry = trait.associatedRcsSensors.add(vendorId=vendor_id, productId=product_id)
        entry.deviceId.resourceId = sensor_id

    return mutate


def unpair_sensor(sensor_id: str) -> Callable[[hvac_pb2.RemoteComfortSensingSettingsTrait], None]:
    """Mutation: remove a sensor from the thermostat's paired sensors.

    Refused while the thermostat uses the sensor (now, or in its sensor
    schedule): pick another sensor first, so it's never left pointing at a
    sensor it no longer has.
    """

    def mutate(trait: hvac_pb2.RemoteComfortSensingSettingsTrait) -> None:
        if trait.activeRcsSelection.activeRcsSensor.resourceId == sensor_id:
            raise ValueError("that sensor is the active one; select another sensor first")
        for interval in trait.rcsControlSchedule.intervals.values():
            if interval.rcsSelection.activeRcsSensor.resourceId == sensor_id:
                raise ValueError("that sensor is used in the sensor schedule; change the schedule first")
        kept = [s for s in trait.associatedRcsSensors if s.deviceId.resourceId != sensor_id]
        if len(kept) != len(trait.associatedRcsSensors):
            del trait.associatedRcsSensors[:]
            trait.associatedRcsSensors.extend(kept)

    return mutate



# --- Experimental: settings the app doesn't expose ------------------------------
#
# These exist in the schema and on the thermostats, but the app never writes
# them, so there is no reference for valid values. Guards are deliberately
# loose sanity checks; whether the firmware honors a value is found by
# writing it and watching what the thermostat confirms and does.


def set_multi_sensor(
    sensor_ids: list[str], *, set_source: bool = True
) -> Callable[[hvac_pb2.RemoteComfortSensingSettingsTrait], None]:
    """Mutation (experimental): average several paired sensors.

    Fills multiSensorSettings and, with set_source, switches the active
    selection to MULTI_SENSOR (held). Undo with select_sensor(...), plus
    clear_multi_sensor().

    Observed on 3rd-gen firmware (2026-10-04): switching the source right away
    makes the thermostat control to 0.0 °C for ~19 s until its first average
    (24.49 °C, between the sensors' readings) arrives.
    """
    if len(set(sensor_ids)) < 2:
        raise ValueError("averaging needs at least two sensors")

    def mutate(trait: hvac_pb2.RemoteComfortSensingSettingsTrait) -> None:
        paired = {s.deviceId.resourceId for s in trait.associatedRcsSensors}
        missing = [s for s in sensor_ids if s not in paired]
        if missing:
            raise ValueError(f"not paired with this thermostat: {missing}")
        ms = trait.multiSensorSettings
        ms.multiSensorEnabled = True
        del ms.multiSensorGroup[:]
        for sid in sensor_ids:
            ms.multiSensorGroup.add(resourceId=sid)
        if set_source:
            trait.activeRcsSelection.rcsSourceType = RCS.RCS_SOURCE_TYPE_MULTI_SENSOR
            trait.activeRcsSelection.ClearField("activeRcsSensor")
            trait.rcsControlMode = RCS.RCS_CONTROL_MODE_HOLD

    return mutate


def clear_multi_sensor() -> Callable[[hvac_pb2.RemoteComfortSensingSettingsTrait], None]:
    """Mutation: disable the multi-sensor group, in the server's form
    (multiSensorEnabled false, empty group; observed 2026-10-04)."""

    def mutate(trait: hvac_pb2.RemoteComfortSensingSettingsTrait) -> None:
        trait.multiSensorSettings.multiSensorEnabled = False
        del trait.multiSensorSettings.multiSensorGroup[:]

    return mutate


SENSOR_PERIODS = ("morning", "midday", "evening", "night")
# The app's fixed period starts (7:00, 11:00, 16:00, 21:00). Tested 2026-10-04:
# the firmware ignores schedules with other times or another number of periods
# (it keeps its last selection, or falls back to its own sensor).
_DEFAULT_PERIOD_STARTS = (7 * 3600, 11 * 3600, 16 * 3600, 21 * 3600)


def set_sensor_schedule(
    choices: dict[str, str], *, standard_times: bool = False
) -> Callable[[hvac_pb2.RemoteComfortSensingSettingsTrait], None]:
    """Mutation: which sensor each of the 4 fixed periods uses.

    ``choices`` maps "morning"/"midday"/"evening"/"night" to THERMOSTAT_SENSOR
    or a paired sensor's resource ID; periods not given keep their sensor.
    The period times are kept; a thermostat without a schedule, or
    standard_times=True, gets the app's standard ones (any period not in
    ``choices`` then uses the thermostat's own sensor).
    """
    unknown = set(choices) - set(SENSOR_PERIODS)
    if unknown:
        raise ValueError(f"periods are {SENSOR_PERIODS}, not {sorted(unknown)}")

    def mutate(trait: hvac_pb2.RemoteComfortSensingSettingsTrait) -> None:
        paired = {s.deviceId.resourceId for s in trait.associatedRcsSensors}
        intervals = trait.rcsControlSchedule.intervals
        if standard_times or sorted(intervals) != [0, 1, 2, 3]:
            for key in list(intervals):
                del intervals[key]
            for i, start in enumerate(_DEFAULT_PERIOD_STARTS):
                intervals[i].startSecondsInDay = start + 1
                intervals[i].endSecondsInDay = _DEFAULT_PERIOD_STARTS[(i + 1) % 4]
                intervals[i].rcsSelection.rcsSourceType = RCS.RCS_SOURCE_TYPE_BACKPLATE
        for i, period in enumerate(SENSOR_PERIODS):
            if period not in choices:
                continue
            selection = intervals[i].rcsSelection
            choice = choices[period]
            if choice == THERMOSTAT_SENSOR:
                selection.rcsSourceType = RCS.RCS_SOURCE_TYPE_BACKPLATE
                selection.ClearField("activeRcsSensor")
            elif choice in paired:
                selection.rcsSourceType = RCS.RCS_SOURCE_TYPE_SINGLE_SENSOR
                selection.activeRcsSensor.resourceId = choice
            else:
                raise ValueError(f"{choice} is not paired with this thermostat")

    return mutate


# --- Humidifier (observed from the app, 2026-10-04) ------------------------------

EQUIPMENT_LABEL = "equipment_settings"
_EQ = hvac_pb2.EquipmentSettingsTrait
# App: "only with heat" = bypass (uses the furnace's warm air);
# "always" = steam generating (makes its own humidity).
HUMIDIFIER_TYPES = {"bypass": _EQ.HUMIDIFIER_TYPE_BYPASS, "steam": _EQ.HUMIDIFIER_TYPE_STEAM_GENERATING}


def set_humidifier(
    *, kind: str | None = None, run_fan: bool | None = None
) -> Callable[[hvac_pb2.EquipmentSettingsTrait], None]:
    """Mutation: humidifier type and whether the fan runs while humidifying.

    equipment_settings holds the whole installer configuration; read-modify-
    write changes only these two fields.
    """
    if kind is not None and kind not in HUMIDIFIER_TYPES:
        raise ValueError(f"humidifier type must be one of {sorted(HUMIDIFIER_TYPES)}")

    def mutate(trait: hvac_pb2.EquipmentSettingsTrait) -> None:
        if kind is not None:
            trait.humidifierType = HUMIDIFIER_TYPES[kind]
        if run_fan is not None:
            trait.humidifierFanActivation = (
                _EQ.HUMIDIFIER_FAN_TYPE_ACTIVATE if run_fan else _EQ.HUMIDIFIER_FAN_TYPE_DONT_ACTIVATE
            )

    return mutate


def set_humidity_control(on: bool) -> Callable[[hvac_pb2.HumidityControlSettingsTrait], None]:
    """Mutation: humidity control on/off (the target value is kept)."""

    def mutate(trait: hvac_pb2.HumidityControlSettingsTrait) -> None:
        trait.targetHumidity.enabled = on

    return mutate


DISPLAY_FARSIGHT = {
    "target": hvac_pb2.DisplaySettingsTrait.FARSIGHT_DISPLAY_TARGET_TEMP,
    "current": hvac_pb2.DisplaySettingsTrait.FARSIGHT_DISPLAY_CURRENT_TEMP,
    "digital_clock": hvac_pb2.DisplaySettingsTrait.FARSIGHT_DISPLAY_DIGITAL_CLOCK,
    "analog_clock": hvac_pb2.DisplaySettingsTrait.FARSIGHT_DISPLAY_ANALOG_CLOCK,
    "weather": hvac_pb2.DisplaySettingsTrait.FARSIGHT_DISPLAY_WEATHER,
    "none": hvac_pb2.DisplaySettingsTrait.FARSIGHT_DISPLAY_NONE,
}


def set_farsight(option: str) -> Callable[[hvac_pb2.DisplaySettingsTrait], None]:
    """Mutation: what the screen shows from across the room (the app's 6 options)."""
    if option not in DISPLAY_FARSIGHT:
        raise ValueError(f"Farsight must be one of {sorted(DISPLAY_FARSIGHT)}")

    def mutate(trait: hvac_pb2.DisplaySettingsTrait) -> None:
        trait.farsightDisplay = DISPLAY_FARSIGHT[option]

    return mutate


# --- Weekly schedules (observed from the app, 2026-10-04) --------------------------
#
# Each of heat/cool/range has its own trait: a map of entries, each with a day,
# time and target(s). On every edit the app rewrites the whole map, numbering
# entries 0..n-1 Monday-first then by time, and fills in a blank
# originalActorInfo as "nobody at time zero". New entries get you-now as both
# current and original actor; a changed entry keeps its original actor.
# Single-target entries carry the app's placeholder for the other target.

from .proto_gen.weave import common_pb2  # noqa: E402

SCHEDULE_LABELS = {
    "heat": "heat_schedule_settings",
    "cool": "cool_schedule_settings",
    "range": "range_schedule_settings",
}
_DOW = common_pb2.DayOfWeek
DAYS = {
    "mon": _DOW.DAY_OF_WEEK_MONDAY, "tue": _DOW.DAY_OF_WEEK_TUESDAY,
    "wed": _DOW.DAY_OF_WEEK_WEDNESDAY, "thu": _DOW.DAY_OF_WEEK_THURSDAY,
    "fri": _DOW.DAY_OF_WEEK_FRIDAY, "sat": _DOW.DAY_OF_WEEK_SATURDAY,
    "sun": _DOW.DAY_OF_WEEK_SUNDAY,
}
_DAY_ORDER = {enum: i for i, enum in enumerate(DAYS.values())}  # the app's Monday-first order
_SCHEDULE_TYPES = {"heat": SP.SET_POINT_TYPE_HEAT, "cool": SP.SET_POINT_TYPE_COOL, "range": SP.SET_POINT_TYPE_RANGE}
_PLACEHOLDER_HEAT_C = 9.0  # heating target the app stores in cool-only entries
_PLACEHOLDER_COOL_C = 32.0  # cooling target the app stores in heat-only entries
SCHEDULE_MIN_F, SCHEDULE_MAX_F = 50, 90  # the app's slider range
SCHEDULE_STEP_S = 15 * 60  # app times are on quarter hours


def _check_schedule_time(day: str, seconds: int) -> None:
    if day not in DAYS:
        raise ValueError(f"day must be one of {list(DAYS)}")
    if not 0 <= seconds < 86400 or seconds % SCHEDULE_STEP_S:
        raise ValueError("time must be within the day, on a quarter hour")


def _check_schedule_temps(kind: str, heat: float | None, cool: float | None) -> None:
    lo, hi = _f_to_c(SCHEDULE_MIN_F), _f_to_c(SCHEDULE_MAX_F)
    needed = {"heat": ("heat",), "cool": ("cool",), "range": ("heat", "cool")}[kind]
    values = {"heat": heat, "cool": cool}
    for name in needed:
        v = values[name]
        if v is None:
            raise ValueError(f"a {kind} schedule entry needs a {name} temperature")
        if not math.isfinite(v) or not lo - 0.01 <= v <= hi + 0.01:
            raise ValueError(f"schedule temperatures must be {SCHEDULE_MIN_F}-{SCHEDULE_MAX_F}°F")
    if kind == "range" and heat >= cool:
        raise ValueError("in a range entry the heat temperature must be below the cool temperature")


def _entry_key(sp: SP.TemperatureSetPoint) -> tuple[int, int]:
    return _DAY_ORDER.get(sp.dayOfWeek, 99), sp.secondsInDay


def _find_entry(trait: SP, day: str, seconds: int) -> int:
    for key, sp in trait.setpoints.items():
        if sp.dayOfWeek == DAYS[day] and sp.secondsInDay == seconds:
            return key
    raise ValueError(f"no schedule entry on {day} at {seconds // 3600:02d}:{seconds % 3600 // 60:02d}")


def _renumber(trait: SP) -> None:
    """Rewrite the map the way the app does after any edit."""
    entries = []
    for sp in trait.setpoints.values():
        copy = SP.TemperatureSetPoint()
        copy.CopyFrom(sp)
        if copy.originalActorInfo == hvac_pb2.HvacActor.HvacActorStruct():
            copy.originalActorInfo.method = hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_NOBODY
            copy.originalActorInfo.timeOfAction.SetInParent()
        entries.append(copy)
    entries.sort(key=_entry_key)
    trait.setpoints.clear()
    for i, sp in enumerate(entries):
        trait.setpoints[i].CopyFrom(sp)


def _set_targets(sp: SP.TemperatureSetPoint, kind: str, heat: float | None, cool: float | None) -> None:
    sp.setpointType = _SCHEDULE_TYPES[kind]
    sp.heatingTarget.value = heat if kind in ("heat", "range") else _PLACEHOLDER_HEAT_C
    sp.coolingTarget.value = cool if kind in ("cool", "range") else _PLACEHOLDER_COOL_C


def add_schedule_entry(
    kind: str, day: str, seconds: int, *, heat: float | None = None, cool: float | None = None,
    user: str | None = None, when: datetime | None = None, method: int = ACTOR_WEB,
) -> Callable[[SP], None]:
    """Mutation: add an entry to the heat, cool or range schedule (temps in °C)."""
    _check_schedule_time(day, seconds)
    _check_schedule_temps(kind, heat, cool)

    def mutate(trait: SP) -> None:
        if any(sp.dayOfWeek == DAYS[day] and sp.secondsInDay == seconds for sp in trait.setpoints.values()):
            raise ValueError("there is already an entry at that day and time")
        sp = trait.setpoints[max(trait.setpoints, default=-1) + 1]
        sp.dayOfWeek = DAYS[day]
        sp.secondsInDay = seconds
        _set_targets(sp, kind, heat, cool)
        stamp_actor(sp.currentActorInfo, user, when, method)
        sp.originalActorInfo.CopyFrom(sp.currentActorInfo)
        _renumber(trait)

    return mutate


def update_schedule_entry(
    kind: str, day: str, seconds: int, *, new_day: str | None = None, new_seconds: int | None = None,
    heat: float | None = None, cool: float | None = None,
    user: str | None = None, when: datetime | None = None, method: int = ACTOR_WEB,
) -> Callable[[SP], None]:
    """Mutation: change an entry's time and/or temperature(s); unspecified ones are kept."""
    _check_schedule_time(day, seconds)
    if new_day is not None or new_seconds is not None:
        _check_schedule_time(new_day or day, new_seconds if new_seconds is not None else seconds)

    def mutate(trait: SP) -> None:
        sp = trait.setpoints[_find_entry(trait, day, seconds)]
        h = heat if heat is not None else (sp.heatingTarget.value if kind in ("heat", "range") else None)
        c = cool if cool is not None else (sp.coolingTarget.value if kind in ("cool", "range") else None)
        _check_schedule_temps(kind, h, c)
        target = (DAYS[new_day or day], new_seconds if new_seconds is not None else seconds)
        if target != (sp.dayOfWeek, sp.secondsInDay) and any(
            (o.dayOfWeek, o.secondsInDay) == target for o in trait.setpoints.values()
        ):
            raise ValueError("there is already an entry at that day and time")
        sp.dayOfWeek, sp.secondsInDay = target
        _set_targets(sp, kind, h, c)
        stamp_actor(sp.currentActorInfo, user, when, method)
        _renumber(trait)

    return mutate


def remove_schedule_entry(kind: str, day: str, seconds: int) -> Callable[[SP], None]:
    """Mutation: delete an entry."""
    _check_schedule_time(day, seconds)

    def mutate(trait: SP) -> None:
        del trait.setpoints[_find_entry(trait, day, seconds)]
        _renumber(trait)

    return mutate


# --- Filter reminder (observed from the app, 2026-10-04) ----------------------------
#
# The reminder is based on HVAC runtime since the filter was changed, which the
# thermostat recalculates from its own history whenever filterChangedDate
# changes. The app picks a month (stored as the 15th, 00:00 UTC) or "more than
# 6 months ago" (stored as 1970-01-01T00:00:01). The trigger is
# filterReplacementThreshold (runtime; 500 h by default), which the app doesn't
# expose.

FILTER_LABEL = "filter_reminder_settings"
FILTER_OVER_6_MONTHS = 1  # the app's "more than 6 months ago": epoch + 1 s


def set_filter_changed(when: datetime | None) -> Callable[[hvac_pb2.FilterReminderSettingsTrait], None]:
    """Mutation: when the filter was last changed. None = the app's "more than 6 months ago"."""

    def mutate(trait: hvac_pb2.FilterReminderSettingsTrait) -> None:
        if when is None:
            trait.filterChangedDate.FromSeconds(FILTER_OVER_6_MONTHS)
        else:
            trait.filterChangedDate.FromDatetime(when.replace(microsecond=0))

    return mutate


def set_filter_threshold(hours: float) -> Callable[[hvac_pb2.FilterReminderSettingsTrait], None]:
    """Mutation (not in the app): runtime hours after which the reminder triggers."""
    if not 50 <= hours <= 10000:
        raise ValueError("filter threshold must be 50-10000 runtime hours")

    def mutate(trait: hvac_pb2.FilterReminderSettingsTrait) -> None:
        trait.filterReplacementThreshold.FromSeconds(int(hours * 3600))

    return mutate
