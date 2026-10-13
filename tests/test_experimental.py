"""Multi-sensor averaging (not in the app) and sensor schedule periods."""

import pytest

from pynestproto import codec
from pynestproto.observe import CONFIRMED, TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.write import (
    THERMOSTAT_SENSOR,
    clear_multi_sensor,
    set_multi_sensor,
    set_sensor_schedule,
)

RCS = hvac_pb2.RemoteComfortSensingSettingsTrait
from .home import ROOM3, ROOM2, ROOM1, THERMOSTAT3
H = 3600


def mutated(trait, mutate):
    out = type(trait)()
    out.CopyFrom(trait)
    mutate(out)
    return out


def test_sensor_schedule_reproduces_the_apps_periods(capture):
    """Last night the app assigned sensors to a thermostat's 4 periods; building the
    same periods must give exactly the schedule the thermostat confirmed."""
    cache = TraitCache()
    target = None
    for raw in codec.parse_stream_body(capture("observe-20261004-001420.bin")):
        for ch in cache.apply_frame(codec.ObserveFrame.FromString(raw)):
            if (ch.resource_id, ch.label, ch.state_type) != (THERMOSTAT3, "remote_comfort_sensing_settings", CONFIRMED):
                continue
            intervals = ch.new.message.rcsControlSchedule.intervals
            if len(intervals) == 4 and all(i.rcsSelection.activeRcsSensor.resourceId for i in intervals.values()):
                target = ch.new.message  # all four periods assigned to sensors
                before = ch.old.message
                break
        if target:
            break
    assert target is not None
    choices = {"morning": ROOM3, "midday": ROOM1, "evening": ROOM2, "night": ROOM3}
    built = mutated(before, set_sensor_schedule(choices))
    assert built.rcsControlSchedule == target.rcsControlSchedule


def paired(*ids):
    t = RCS()
    for sid in ids:
        t.associatedRcsSensors.add(vendorId=9050, productId=26).deviceId.resourceId = sid
    return t


def test_sensor_schedule_creates_app_layout_and_keeps_times():
    t = mutated(paired(ROOM1), set_sensor_schedule({"midday": ROOM1}))
    iv = t.rcsControlSchedule.intervals
    assert [(iv[i].startSecondsInDay, iv[i].endSecondsInDay) for i in range(4)] == [
        (7 * H + 1, 11 * H), (11 * H + 1, 16 * H), (16 * H + 1, 21 * H), (21 * H + 1, 7 * H)]
    assert iv[1].rcsSelection.activeRcsSensor.resourceId == ROOM1
    assert iv[0].rcsSelection.rcsSourceType == RCS.RCS_SOURCE_TYPE_BACKPLATE
    t2 = mutated(t, set_sensor_schedule({"midday": THERMOSTAT_SENSOR}))  # times untouched
    assert t2.rcsControlSchedule.intervals[1].startSecondsInDay == 11 * H + 1
    assert not t2.rcsControlSchedule.intervals[1].rcsSelection.HasField("activeRcsSensor")
    with pytest.raises(ValueError, match="not paired"):
        mutated(paired(ROOM1), set_sensor_schedule({"night": ROOM2}))
    with pytest.raises(ValueError, match="periods"):
        set_sensor_schedule({"brunch": ROOM1})


def test_multi_sensor_sets_group_and_source():
    t = mutated(paired(ROOM1, ROOM2, ROOM3), set_multi_sensor([ROOM1, ROOM2]))
    assert t.multiSensorSettings.multiSensorEnabled
    assert [r.resourceId for r in t.multiSensorSettings.multiSensorGroup] == [ROOM1, ROOM2]
    assert t.activeRcsSelection.rcsSourceType == RCS.RCS_SOURCE_TYPE_MULTI_SENSOR
    assert not t.activeRcsSelection.HasField("activeRcsSensor")
    cleared = mutated(t, clear_multi_sensor()).multiSensorSettings
    assert not cleared.multiSensorEnabled and len(cleared.multiSensorGroup) == 0


def test_multi_sensor_validation():
    with pytest.raises(ValueError, match="two"):
        set_multi_sensor([ROOM1, ROOM1])
    with pytest.raises(ValueError, match="not paired"):
        mutated(paired(ROOM1), set_multi_sensor([ROOM1, ROOM2]))
