"""The Home Assistant-run target sensor schedule."""

from datetime import datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.protonest.const import DOMAIN
from custom_components.protonest.pynestproto.write import RCS, THERMOSTAT_SENSOR
from custom_components.protonest.sensor_schedule import Entry, due_entry, next_change, validate
from tests.home import ROOM1, ROOM1_NAME, ROOM2, ROOM2_NAME, THERMOSTAT3, THERMOSTAT3_NAME
from tests_ha.test_setup import T3, call, sensor_entity, setup

LABEL = "remote_comfort_sensing_settings"
A = Entry("mon", 7 * 60, ("a",))
B = Entry("mon", 22 * 60, ("a", "b"))
C = Entry("wed", 12 * 60, (THERMOSTAT_SENSOR,))
WEEK = [A, B, C]


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A local time in the week of Monday 2026-10-12."""
    return datetime(2026, 10, 12 + day, hour, minute, tzinfo=dt_util.get_default_time_zone())


# --- pure logic --------------------------------------------------------------------


@pytest.mark.parametrize(("now", "due"), [
    (at(0, 7), A), (at(0, 6, 59), C),  # before Monday's first entry: last week's last one
    (at(1, 10), B), (at(2, 11, 59), B), (at(2, 12), C), (at(6, 23, 59), C),
])
def test_due_entry(now, due):
    assert due_entry(WEEK, now) == due


@pytest.mark.parametrize(("now", "wake"), [
    (at(0, 7), at(0, 22)), (at(2, 13), at(7, 7)), (at(6, 23), at(7, 7)),
])
def test_next_change(now, wake):
    assert next_change(WEEK, now) == wake


def test_no_entries():
    assert due_entry([], at(0, 7)) is None and next_change([], at(0, 7)) is None


@pytest.mark.parametrize(("entries", "error"), [
    ([Entry("mon", 60, (THERMOSTAT_SENSOR, "a"))], "can't be averaged"),
    ([Entry("mon", 60, ("x",))], "not paired"),
    ([Entry("mon", 60, ("a",)), Entry("mon", 60, ("b",))], "two entries"),
    ([Entry("mon", 60, ())], "choose"),
    ([Entry("mon", 60, ("a", "a"))], "choose"),
    ([Entry("xyz", 60, ("a",))], "invalid"),
])
def test_validate_rejects(entries, error):
    with pytest.raises(ValueError, match=error):
        validate(entries, ["a", "b"])


def test_validate_sorts():
    assert validate([C, A, B], ["a", "b"]) == WEEK


# --- in Home Assistant ----------------------------------------------------------------


def rcs_writes(client):
    return [new for rid, label, new in client.writes if rid == THERMOSTAT3 and label == LABEL]


async def test_schedule_runs_switches_and_averages(hass, fake_client, freezer, hass_storage):
    freezer.move_to(at(0, 10))  # Monday 10:00
    entry, client = await setup(hass, fake_client)
    climate, switch = f"climate.{T3}_thermostat", f"switch.{T3}_target_sensor_schedule"
    room1, room2 = sensor_entity(ROOM1_NAME, "temperature"), sensor_entity(ROOM2_NAME, "temperature")

    await call(hass, DOMAIN, "set_sensor_schedule", climate, entries=[
        {"day": "mon", "time": "09:00", "sensors": [room1]},
        {"day": "mon", "time": "11:00", "sensors": [room1, room2]},
        {"day": "mon", "time": "22:00", "sensors": ["thermostat"]},
    ])
    attr = hass.states.get(climate).attributes["sensor_schedule"]
    assert attr["enabled"] is False and attr["switch"] == switch
    assert attr["entries"][1] == {"day": "mon", "time": "11:00", "sensors": [room1, room2]}
    assert {o["id"] for o in attr["options"]} >= {"thermostat", room1, room2}
    assert not rcs_writes(client), "nothing happens while the schedule is off"

    # Turning it on applies the entry due now.
    await call(hass, "switch", "turn_on", switch)
    assert hass.states.get(switch).state == "on"
    rcs = rcs_writes(client)[-1]
    assert rcs.activeRcsSelection.activeRcsSensor.resourceId == ROOM1
    assert rcs.rcsControlMode == RCS.RCS_CONTROL_MODE_HOLD

    # A manual pick holds: editing the schedule doesn't re-apply the same due entry.
    await call(hass, "select", "select_option", f"select.{T3}_target_sensor", option="Thermostat")
    count = len(rcs_writes(client))
    await call(hass, DOMAIN, "set_sensor_schedule", climate, entries=[
        *hass.states.get(climate).attributes["sensor_schedule"]["entries"],
        {"day": "tue", "time": "08:00", "sensors": [room2]},
    ])
    assert len(rcs_writes(client)) == count

    # At 11:00 it switches to averaging the two sensors.
    freezer.move_to(at(0, 11, 0) + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    rcs = rcs_writes(client)[-1]
    assert rcs.activeRcsSelection.rcsSourceType == RCS.RCS_SOURCE_TYPE_MULTI_SENSOR
    assert {s.resourceId for s in rcs.multiSensorSettings.multiSensorGroup} == {ROOM1, ROOM2}

    # It's saved, and after a reload it neither forgets nor re-applies.
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    saved = hass_storage[f"{DOMAIN}.sensor_schedule.{entry.entry_id}"]["data"]["thermostats"][THERMOSTAT3]
    assert saved["enabled"] and len(saved["entries"]) == 4 and saved["applied"]["time"] == "11:00"
    count = len(rcs_writes(client))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    client = fake_client.instances[-1]
    assert hass.states.get(switch).state == "on"
    assert not rcs_writes(client), "the due entry was already applied before the reload"


async def test_schedule_rejects_bad_entries(hass, fake_client):
    await setup(hass, fake_client)
    from homeassistant.exceptions import ServiceValidationError

    climate = f"climate.{T3}_thermostat"
    with pytest.raises(ServiceValidationError, match="can't be averaged"):
        await call(hass, DOMAIN, "set_sensor_schedule", climate, entries=[
            {"day": "mon", "time": "09:00", "sensors": ["thermostat", sensor_entity(ROOM1_NAME, "temperature")]}])
    with pytest.raises(ServiceValidationError, match="not a Nest Temperature Sensor"):
        await call(hass, DOMAIN, "set_sensor_schedule", climate, entries=[
            {"day": "mon", "time": "09:00", "sensors": [f"sensor.{T3}_humidity"]}])
    assert hass.states.get(climate).attributes["sensor_schedule"]["entries"] == []
