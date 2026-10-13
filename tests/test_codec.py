"""Codec tests against real captured traffic.

``nest_proto`` is a captured Observe response body (base64-decoded): 16 frames
from a snapshot of the author's home. ``heat_update_request.bin`` /
``heat_update_response.bin`` are a captured BatchUpdateState round-trip that set
a thermostat's heating target from the web app.
"""

import pytest
from google.protobuf import timestamp_pb2

from pynestproto import codec
from pynestproto.proto_gen.google.rpc import stream_body_pb2
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.proto_gen.nestlabs.gateway import v1_pb2, v2_pb2

from .home import THERMOSTAT2, THERMOSTAT1, USER
from .home import ROOM4

# Present in the capture but absent from the vendored schema. None of them is
# needed for thermostat control. If this set changes after a schema update,
# review it and update the expectation.
KNOWN_UNRESOLVED = {
    "nest.trait.hvac.ScheduleOffsetTrait",
    "nest.trait.hvac.ScheduleOffsetSettingsTrait",
    "nest.trait.user.UserSmokeCoNotificationSettingsTrait",
}

TempSetPoint = hvac_pb2.SetPointScheduleSettingsTrait
Fan = hvac_pb2.FanControlSettingsTrait
FanSpeed = hvac_pb2.FanControlTrait.FanSpeedSetting


@pytest.fixture(scope="module")
def frames(capture):
    return codec.parse_observe(capture("nest_proto"))


def trait_states(frames):
    return [ts for f in frames for ts in f.traitStates]


def confirmed_traits(frames, resource_id):
    """Map trait label -> decoded CONFIRMED state, latest monotonicVersion wins."""
    latest = {}
    for ts in trait_states(frames):
        if ts.traitId.resourceId != resource_id:
            continue
        if v2_pb2.StateType.CONFIRMED not in ts.stateTypes:
            continue
        label = ts.traitId.traitLabel
        if label not in latest or ts.monotonicVersion >= latest[label].monotonicVersion:
            latest[label] = ts
    return {label: codec.unpack(ts.patch.values) for label, ts in latest.items()}


# --- StreamBody envelope ---------------------------------------------------


def test_stream_body_error_status_raises():
    body = stream_body_pb2.StreamBody()
    body.status.code = 16
    body.status.message = "UNAUTHENTICATED"
    with pytest.raises(codec.StreamError) as err:
        codec.parse_stream_body(body.SerializeToString())
    assert err.value.code == 16


def test_stream_body_skips_noop_keepalives():
    body = stream_body_pb2.StreamBody(message=[b"\x01"], noop=[b"\x00\x00"])
    assert codec.parse_stream_body(body.SerializeToString()) == [b"\x01"]


# --- Observe capture -------------------------------------------------------


def test_observe_capture_structure(frames):
    # One Observe response body is many frames, not one ObserveResponse.
    assert len(frames) == 16
    resources = {rm.resourceId: rm.type for f in frames for rm in f.resourceMetas}
    assert len(resources) == 10
    types = sorted(resources.values())
    assert types.count("nest.resource.NestLearningThermostat3Resource") == 3
    assert types.count("nest.resource.NestKryptoniteResource") == 5
    assert len(trait_states(frames)) == 533


def test_trait_payloads_resolve(frames):
    unresolved = set()
    resolved = 0
    for ts in trait_states(frames):
        if codec.unpack(ts.patch.values) is None:
            unresolved.add(codec.trait_type_name(ts.patch.values))
        else:
            resolved += 1
    assert resolved == 523
    assert unresolved == KNOWN_UNRESOLVED


def test_unknown_type_returns_none():
    any_msg = codec.pack(hvac_pb2.EcoModeTrait())
    any_msg.type_url = codec.TYPE_URL_PREFIX + "does.not.Exist"
    assert codec.unpack(any_msg) is None


def test_thermostat2_values(frames):
    t = confirmed_traits(frames, THERMOSTAT2)

    assert t["current_temperature"].temperatureValue.temperature.value == pytest.approx(19.589996)
    assert t["current_humidity"].humidityValue.humidity.value == pytest.approx(33.5)

    target = t["target_temperature_settings"]
    assert target.enabled.value is True
    assert target.targetTemperature.setpointType == TempSetPoint.SET_POINT_TYPE_HEAT
    assert target.targetTemperature.heatingTarget.value == pytest.approx(19.388885)
    assert target.targetTemperature.coolingTarget.value == pytest.approx(20.734177)

    assert t["humidity_control_settings"].targetHumidity.value == pytest.approx(35.000004)

    fan = t["fan_control_settings"]
    assert fan.mode == Fan.FAN_MODE_DUTY_CYCLE
    assert fan.hvacOverrideSpeed == FanSpeed.FAN_SPEED_SETTING_AUTO
    assert fan.timerSpeed == FanSpeed.FAN_SPEED_SETTING_STAGE1
    assert t["fan_control"].currentSpeed == FanSpeed.FAN_SPEED_SETTING_OFF

    assert t["eco_mode_state"].ecoMode == hvac_pb2.EcoModeStateTrait.ECO_MODE_INACTIVE
    assert (
        t["display_settings"].temperatureScale
        == hvac_pb2.DisplaySettingsTrait.TEMPERATURE_SCALE_F
    )

    rcs = t["remote_comfort_sensing_settings"]
    rcs_cls = hvac_pb2.RemoteComfortSensingSettingsTrait
    assert rcs.activeRcsSelection.rcsSourceType == rcs_cls.RCS_SOURCE_TYPE_SINGLE_SENSOR
    assert rcs.activeRcsSelection.activeRcsSensor.resourceId == ROOM4
    assert [s.deviceId.resourceId for s in rcs.associatedRcsSensors] == [ROOM4]


# --- BatchUpdateState write round-trip ---------------------------------------


def test_write_request_round_trips_byte_identical(capture):
    raw = capture("heat_update_request.bin")
    req = v1_pb2.BatchUpdateStateRequest.FromString(raw)
    assert req.SerializeToString() == raw


def test_write_request_built_from_scratch_matches_capture(capture):
    """Build the captured write field by field, as the client will."""
    setpoint = TempSetPoint.TemperatureSetPoint(
        setpointType=TempSetPoint.SET_POINT_TYPE_HEAT,
    )
    setpoint.heatingTarget.value = 19.91452
    setpoint.coolingTarget.value = 29.91365
    actor = setpoint.currentActorInfo
    actor.method = hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_WEB
    actor.originator.resourceId = USER
    actor.timeOfAction.FromJsonString("2026-01-25T22:31:39Z")
    # The web app sends originalActorInfo with present-but-empty submessages.
    setpoint.originalActorInfo.originator.SetInParent()
    setpoint.originalActorInfo.timeOfAction.CopyFrom(timestamp_pb2.Timestamp())
    setpoint.originalActorInfo.timeOfAction.SetInParent()

    trait = hvac_pb2.TargetTemperatureSettingsTrait(targetTemperature=setpoint)
    trait.enabled.value = True

    update = v1_pb2.TraitUpdateStateRequest(state=codec.pack(trait))
    update.traitRequest.resourceId = THERMOSTAT1
    update.traitRequest.traitLabel = "target_temperature_settings"
    update.traitRequest.requestId = "e739720f-e7c3-4e39-8b29-4e6aed61fc83"
    req = v1_pb2.BatchUpdateStateRequest(batchUpdateStateRequest=[update])

    assert req.SerializeToString() == capture("heat_update_request.bin")


def test_write_response(capture):
    frames = codec.parse_update_state(capture("heat_update_response.bin"))
    assert len(frames) == 1
    (op,) = frames[0].traitOperations
    assert op.traitRequest.resourceId == THERMOSTAT1
    assert op.traitRequest.traitLabel == "target_temperature_settings"
    assert op.progress == v1_pb2.TraitOperation.COMPLETE
    assert op.status.code == 0
