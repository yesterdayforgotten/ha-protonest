"""Humidity, target-sensor and eco writes.

Replays two live recordings made while the Nest app changed these settings
(2026-10-03 and 2026-10-04). For every change the app wrote, applying our
mutation to the state before it must give exactly the state the thermostat
confirmed. Changes the thermostat made on its own (e.g. picking the sensor for
the current schedule period, or tidying the eco actor) are skipped.
"""

from datetime import UTC

import pytest

from pynestproto import codec
from pynestproto.observe import CONFIRMED, TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.write import (
    SENSOR_SCHEDULE,
    THERMOSTAT_SENSOR,
    select_sensor,
    set_eco,
    set_hvac_mode,
    set_target_humidity,
    set_target_temperature,
)

RCS = hvac_pb2.RemoteComfortSensingSettingsTrait
ECO = hvac_pb2.EcoModeStateTrait
RECORDINGS = ("observe-20261003-224223.bin", "observe-20261004-001420.bin")
USER_METHODS = {
    hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_WEB,
    hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_ANDROID,
    hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_IOS,
}


def mutated(trait, mutate):
    out = type(trait)()
    out.CopyFrom(trait)
    mutate(out)
    return out


def confirmed_changes(capture, label):
    cache = TraitCache()
    for name in RECORDINGS:
        for raw in codec.parse_stream_body(capture(name)):
            for ch in cache.apply_frame(codec.ObserveFrame.FromString(raw)):
                if ch.label == label and ch.state_type == CONFIRMED and not ch.is_new:
                    yield ch.old.message, ch.new.message
        cache = TraitCache()  # recordings are separate sessions


def test_app_humidity_writes_reproduced(capture):
    seen = []
    for old, new in confirmed_changes(capture, "humidity_control_settings"):
        value = round(new.targetHumidity.value)
        assert mutated(old, set_target_humidity(value)) == new
        seen.append(value)
    # 40->20->40 (night 1); 40->10->60->40 (night 2: the app's min and max)
    assert seen == [20, 40, 10, 60, 40]


def rcs_choice(new):
    sel = new.activeRcsSelection
    if sel.rcsSourceType == RCS.RCS_SOURCE_TYPE_BACKPLATE:
        return THERMOSTAT_SENSOR
    return sel.activeRcsSensor.resourceId


def test_app_sensor_selections_reproduced(capture):
    kinds = []
    for old, new in confirmed_changes(capture, "remote_comfort_sensing_settings"):
        if old.rcsControlSchedule != new.rcsControlSchedule:
            continue  # editing schedule periods: not implemented
        if new.rcsControlMode == RCS.RCS_CONTROL_MODE_SCHEDULE:
            if old.rcsControlMode == RCS.RCS_CONTROL_MODE_SCHEDULE:
                continue  # the thermostat applying the current period on its own
            choice = SENSOR_SCHEDULE
        else:
            choice = rcs_choice(new)
        assert mutated(old, select_sensor(choice)) == new
        kinds.append(choice if choice in (THERMOSTAT_SENSOR, SENSOR_SCHEDULE) else "sensor")
    assert {"sensor", THERMOSTAT_SENSOR, SENSOR_SCHEDULE} <= set(kinds)


def test_app_eco_writes_reproduced(capture):
    count = 0
    for old, new in confirmed_changes(capture, "eco_mode_state"):
        if new.ecoMode == old.ecoMode or new.ecoModeActor.method not in USER_METHODS:
            continue  # the thermostat tidying the actor afterwards
        actor = new.ecoModeActor
        mutate = set_eco(
            new.ecoMode == ECO.ECO_MODE_MANUAL_ECO,
            user=actor.originator.resourceId,
            when=actor.timeOfAction.ToDatetime(UTC),
            method=actor.method,
        )
        expected = type(new)()
        expected.CopyFrom(new)
        # The app stamps milliseconds; we stamp whole seconds like the web app.
        expected.ecoModeActor.timeOfAction.nanos = 0
        assert mutated(old, mutate) == expected
        count += 1
    assert count == 2  # on, then off


# --- Validation and edge cases -------------------------------------------------


@pytest.mark.parametrize("bad", [5, 65, 42, 40.0])
def test_humidity_outside_app_range_or_step_is_refused(bad):
    with pytest.raises(ValueError):
        set_target_humidity(bad)


def paired_trait(*sensors, mode=RCS.RCS_CONTROL_MODE_HOLD):
    t = RCS(rcsControlMode=mode)
    for sid in sensors:
        t.associatedRcsSensors.add().deviceId.resourceId = sid
    t.activeRcsSelection.rcsSourceType = RCS.RCS_SOURCE_TYPE_BACKPLATE
    return t


def test_unpaired_sensor_is_refused():
    with pytest.raises(ValueError, match="not paired"):
        mutated(paired_trait("DEVICE_A"), select_sensor("DEVICE_B"))


def test_picking_a_sensor_leaves_schedule_mode():
    t = mutated(paired_trait("DEVICE_A", mode=RCS.RCS_CONTROL_MODE_SCHEDULE), select_sensor("DEVICE_A"))
    assert t.rcsControlMode == RCS.RCS_CONTROL_MODE_HOLD
    assert t.activeRcsSelection.activeRcsSensor.resourceId == "DEVICE_A"


def test_thermostat_sensor_clears_the_sensor_id():
    t = mutated(paired_trait("DEVICE_A"), select_sensor("DEVICE_A"))
    t = mutated(t, select_sensor(THERMOSTAT_SENSOR))
    assert t.activeRcsSelection.rcsSourceType == RCS.RCS_SOURCE_TYPE_BACKPLATE
    assert not t.activeRcsSelection.HasField("activeRcsSensor")


def test_eco_already_in_state_is_a_noop():
    t = ECO(ecoMode=ECO.ECO_MODE_INACTIVE)
    assert mutated(t, set_eco(False, user="USER_X")) == t


SP = hvac_pb2.SetPointScheduleSettingsTrait
MODE_NAMES = {SP.SET_POINT_TYPE_HEAT: "heat", SP.SET_POINT_TYPE_COOL: "cool", SP.SET_POINT_TYPE_RANGE: "range"}


def test_app_setpoint_and_mode_writes_reproduced(capture):
    kinds = []
    for old, new in confirmed_changes(capture, "target_temperature_settings"):
        actor = new.targetTemperature.currentActorInfo
        if actor.method not in USER_METHODS:
            continue  # schedule or device changes
        stamp = dict(user=actor.originator.resourceId or None, when=actor.timeOfAction.ToDatetime(UTC), method=actor.method)
        ot, nt = old.targetTemperature, new.targetTemperature
        if (old.enabled.value, ot.setpointType) != (new.enabled.value, nt.setpointType):
            mutate = set_hvac_mode(MODE_NAMES[nt.setpointType] if new.enabled.value else "off", **stamp)
            kinds.append("mode")
        else:
            heat = nt.heatingTarget.value if nt.heatingTarget.value != ot.heatingTarget.value else None
            cool = nt.coolingTarget.value if nt.coolingTarget.value != ot.coolingTarget.value else None
            mutate = set_target_temperature(heat=heat, cool=cool, **stamp)
            kinds.append("setpoint")
        expected = type(new)()
        expected.CopyFrom(new)
        expected.targetTemperature.currentActorInfo.timeOfAction.nanos = 0  # app stamps ms
        assert mutated(old, mutate) == expected
    # night 1: -1F and back, off and on; night 2: our two writes, another thermostat +1F
    assert kinds.count("mode") == 2 and kinds.count("setpoint") >= 5
