"""Device model over a real every-trait recording (2026-10-04)."""

import pytest

from pynestproto import codec, model
from pynestproto.observe import TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.write import THERMOSTAT_SENSOR, set_sensor_schedule

from .home import (
    ROOM1_NAME, ROOM2_NAME, ROOM3, ROOM3_NAME, ROOM4, ROOM4_NAME, ROOM5_NAME,
    THERMOSTAT1, THERMOSTAT1_NAME, THERMOSTAT1_SERIAL, THERMOSTAT2, THERMOSTAT2_NAME, THERMOSTAT3, THERMOSTAT3_NAME,
)


@pytest.fixture(scope="module")
def cache(capture):
    c = TraitCache()
    for m in codec.parse_stream_body(capture("observe-20261004-080746.bin")):
        c.apply_frame(codec.ObserveFrame.FromString(m))
    return c


def by_name(cache):
    return {t.name: t for t in model.thermostats(cache)}


def test_devices_and_identity(cache):
    t = by_name(cache)
    assert set(t) == {THERMOSTAT1_NAME, THERMOSTAT2_NAME, THERMOSTAT3_NAME}
    assert t[THERMOSTAT1_NAME].model == "Nest Learning Thermostat Display (3rd Generation)"
    assert t[THERMOSTAT1_NAME].serial_number == THERMOSTAT1_SERIAL
    assert all(x.online for x in t.values())
    assert {s.name for s in model.remote_sensors(cache)} == {
        ROOM5_NAME, ROOM2_NAME, ROOM4_NAME, ROOM3_NAME, ROOM1_NAME}


def test_thermostat_readings(cache):
    mf = by_name(cache)[THERMOSTAT1_NAME]
    assert mf.hvac_mode == "cool"
    assert mf.temperature_scale == "F"
    assert mf.can_heat and mf.can_cool and mf.has_fan and mf.fan_speeds == 1
    assert 15 < mf.current_temperature < 35
    assert 3.5 < mf.battery_voltage < 4.2


def test_humidifier_only_on_thermostat1(cache):
    t = by_name(cache)
    assert t[THERMOSTAT1_NAME].has_humidifier and t[THERMOSTAT1_NAME].humidifier_type == "bypass"
    assert not t[THERMOSTAT2_NAME].has_humidifier and t[THERMOSTAT2_NAME].humidifier_type is None


def test_fan_schedule_values(cache):
    up = by_name(cache)[THERMOSTAT3_NAME]
    assert up.fan_schedule_enabled and up.fan_schedule_minutes == 15 and up.fan_schedule_hours == (9, 23)


def test_target_sensor(cache):
    t = by_name(cache)
    assert t[THERMOSTAT2_NAME].active_sensor == ROOM4
    assert t[THERMOSTAT1_NAME].active_sensor == THERMOSTAT_SENSOR
    assert t[THERMOSTAT3_NAME].active_sensor == ROOM3
    assert len(t[THERMOSTAT3_NAME].paired_sensors) == 3 and t[THERMOSTAT3_NAME].has_sensor_schedule


def test_thresholds_and_filter(cache):
    mf = by_name(cache)[THERMOSTAT1_NAME]
    assert mf.threshold("eco_cool") == (True, pytest.approx(29.347618))
    assert mf.threshold("safety_high")[0] is False
    assert mf.filter_threshold_hours == 500
    assert mf.filter_reminder_level in ("none", "first")


def test_schedule_listing_monday_first(cache):
    entries = by_name(cache)[THERMOSTAT1_NAME].schedule("cool")
    assert len(entries) == 8
    assert entries[0][:2] == ("mon", 27000) and entries[0][2] is None
    assert [e[0] for e in entries] == sorted([e[0] for e in entries], key=["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index)


def test_standard_times_reset_rebuilds_app_layout():
    t = hvac_pb2.RemoteComfortSensingSettingsTrait()
    t.associatedRcsSensors.add().deviceId.resourceId = ROOM3
    for i, start in enumerate((18000, 30600, 46800, 72000)):
        t.rcsControlSchedule.intervals[i].startSecondsInDay = start + 1
        t.rcsControlSchedule.intervals[i].rcsSelection.rcsSourceType = 2
        t.rcsControlSchedule.intervals[i].rcsSelection.activeRcsSensor.resourceId = ROOM3
    set_sensor_schedule({}, standard_times=True)(t)
    iv = t.rcsControlSchedule.intervals
    assert [iv[i].startSecondsInDay for i in range(4)] == [25201, 39601, 57601, 75601]
    assert all(not iv[i].rcsSelection.HasField("activeRcsSensor") for i in range(4))


def test_sensor_model_is_readable(cache):
    room1 = next(s for s in model.remote_sensors(cache) if s.name == ROOM1_NAME)
    assert room1.model == "Nest Temperature Sensor" and room1.model_id == "KR1"
    mf = next(t for t in model.thermostats(cache) if t.name == THERMOSTAT1_NAME)
    assert mf.model == mf.model_id == "Nest Learning Thermostat Display (3rd Generation)"
