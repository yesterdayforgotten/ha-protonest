"""Integration setup, entity states, and controls against a real snapshot."""

from datetime import UTC, datetime

import pytest
from homeassistant.config_entries import ConfigEntryState, SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import slugify

from custom_components.protonest.const import CONF_COOKIES, CONF_ISSUE_TOKEN, DOMAIN
from tests.home import (
    ROOM1_NAME, ROOM2_NAME, ROOM3_NAME, ROOM4_NAME, THERMOSTAT1, THERMOSTAT1_NAME, THERMOSTAT2_NAME, THERMOSTAT3_NAME,
    USER_ID,
)

ISSUE = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&x=1"
T1, T2, T3 = (slugify(n) for n in (THERMOSTAT1_NAME, THERMOSTAT2_NAME, THERMOSTAT3_NAME))
THERMOSTAT3_SENSORS = (ROOM1_NAME, ROOM2_NAME, ROOM3_NAME)


def sensor_entity(name: str, key: str) -> str:
    return f"sensor.{slugify(name)}_{key}"


async def setup(hass: HomeAssistant, fake_client):
    hass.config.units = US_CUSTOMARY_SYSTEM  # the user's units; thermostats display °F
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER_ID,
                            data={CONF_ISSUE_TOKEN: ISSUE, CONF_COOKIES: "SID=a; SIDCC=b"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, fake_client.instances[-1]


async def call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)
    await hass.async_block_till_done()


# --- states ----------------------------------------------------------------------


async def test_setup_creates_devices_and_climate(hass, fake_client):
    entry, _ = await setup(hass, fake_client)
    assert entry.state is ConfigEntryState.LOADED
    for name in (T1, T2, T3):
        st = hass.states.get(f"climate.{name}")
        assert st is not None, name
        assert st.state == "cool"
        assert set(st.attributes["hvac_modes"]) == {"off", "heat", "cool", "heat_cool"}
        assert st.attributes["preset_modes"] == ["none", "eco"]
        assert 50 <= st.attributes["temperature"] <= 90  # °F
    assert hass.states.get(sensor_entity(ROOM4_NAME, "temperature")) is not None
    assert hass.states.get(sensor_entity(ROOM1_NAME, "battery_voltage")) is not None


async def test_humidity_only_where_there_is_a_humidifier(hass, fake_client):
    await setup(hass, fake_client)
    assert hass.states.get(f"climate.{T1}").attributes.get("humidity") == 40
    assert "humidity" not in hass.states.get(f"climate.{T2}").attributes
    assert hass.states.get(f"switch.{T1}_humidity_control") is not None
    assert hass.states.get(f"switch.{T2}_humidity_control") is None
    assert hass.states.get(f"select.{T1}_humidifier_operation").state == "Only with heat"


async def test_target_sensor_options(hass, fake_client):
    await setup(hass, fake_client)
    up = hass.states.get(f"select.{T3}_target_sensor")
    assert up.state == ROOM3_NAME
    assert up.attributes["options"] == ["Thermostat", *THERMOSTAT3_SENSORS, "Average of all sensors", "Schedule"]
    base = hass.states.get(f"select.{T2}_target_sensor")
    assert base.attributes["options"] == ["Thermostat", ROOM4_NAME]  # one sensor, no schedule
    assert hass.states.get(f"select.{T3}_sensor_schedule_morning").state == "Thermostat"
    assert hass.states.get(f"select.{T2}_sensor_schedule_morning") is None


async def test_settings_entities(hass, fake_client):
    await setup(hass, fake_client)
    assert hass.states.get(f"select.{T3}_fan_schedule_start").state == "09:00"
    assert hass.states.get(f"select.{T3}_fan_schedule_run_time").state == "15 min per hour"
    assert hass.states.get(f"switch.{T1}_sunblock").state == "on"
    assert hass.states.get(f"number.{T1}_filter_reminder_threshold").state == "500.0"
    assert float(hass.states.get(f"number.{T1}_eco_cooling_limit").state) == pytest.approx(84.8, abs=0.1)
    assert hass.states.get(f"binary_sensor.{T1}_filter_replacement_needed").state == "off"
    assert hass.states.get(f"button.{T1}_filter_replaced") is not None


# --- controls ----------------------------------------------------------------------


async def test_set_temperature_writes_celsius(hass, fake_client):
    _, client = await setup(hass, fake_client)
    await call(hass, "climate", "set_temperature", f"climate.{T1}", temperature=74)
    trait = client.last_write("target_temperature_settings")
    assert trait.targetTemperature.coolingTarget.value == pytest.approx(23.3333, abs=1e-3)
    assert hass.states.get(f"climate.{T1}").attributes["temperature"] == 74


async def test_mode_off_then_heat_supplies_missing_heating_setpoint(hass, fake_client):
    _, client = await setup(hass, fake_client)
    await call(hass, "climate", "set_hvac_mode", f"climate.{T3}", hvac_mode="off")
    assert client.last_write("target_temperature_settings").enabled.value is False
    assert hass.states.get(f"climate.{T3}").state == "off"
    await call(hass, "climate", "set_hvac_mode", f"climate.{T3}", hvac_mode="heat")
    trait = client.last_write("target_temperature_settings")
    assert trait.enabled.value is True
    assert trait.targetTemperature.heatingTarget.value == pytest.approx(20.0)  # was never set
    assert hass.states.get(f"climate.{T3}").attributes["temperature"] == 68


async def test_eco_preset_and_humidity(hass, fake_client):
    _, client = await setup(hass, fake_client)
    await call(hass, "climate", "set_preset_mode", f"climate.{T1}", preset_mode="eco")
    assert client.last_write("eco_mode_state").ecoMode == 2  # MANUAL_ECO
    assert hass.states.get(f"climate.{T1}").attributes["preset_mode"] == "eco"
    await call(hass, "climate", "set_humidity", f"climate.{T1}", humidity=47)
    assert client.last_write("humidity_control_settings").targetHumidity.value == 45  # app's 5% steps


async def test_target_sensor_select_writes(hass, fake_client):
    _, client = await setup(hass, fake_client)
    sel = f"select.{T3}_target_sensor"
    await call(hass, "select", "select_option", sel, option=ROOM1_NAME)
    assert client.last_write("remote_comfort_sensing_settings").activeRcsSelection.activeRcsSensor.resourceId.endswith("B50F")
    await call(hass, "select", "select_option", sel, option="Average of all sensors")
    rcs = client.last_write("remote_comfort_sensing_settings")
    assert len(rcs.multiSensorSettings.multiSensorGroup) == 3
    assert hass.states.get(sel).state == "Average of all sensors"
    await call(hass, "select", "select_option", sel, option="Schedule")
    assert hass.states.get(sel).state == "Schedule"


async def test_switch_number_select_button(hass, fake_client):
    _, client = await setup(hass, fake_client)
    await call(hass, "switch", "turn_off", f"switch.{T1}_sunblock")
    assert client.last_write("sunblock_settings").enabled is False
    await call(hass, "number", "set_value", f"number.{T2}_filter_reminder_threshold", value=1800)
    assert client.last_write("filter_reminder_settings").filterReplacementThreshold.seconds == 1800 * 3600
    await call(hass, "select", "select_option", f"select.{T2}_fan_run_duration", option="45 min")
    assert client.last_write("fan_control_settings").timerDuration.seconds == 2700
    await call(hass, "button", "press", f"button.{T3}_filter_replaced")
    changed = client.last_write("filter_reminder_settings").filterChangedDate.ToDatetime(UTC)
    assert changed.date() == dt_util.now().date()


async def test_actions(hass, fake_client):
    _, client = await setup(hass, fake_client)
    await call(hass, DOMAIN, "run_fan", f"climate.{T2}", duration={"minutes": 45})
    fan = client.last_write("fan_control_settings")
    assert fan.timerDuration.seconds == 2700 and fan.timerEnd.seconds > datetime.now(UTC).timestamp()
    await call(hass, DOMAIN, "schedule_add", f"climate.{T1}", schedule="cool", day="mon", time="12:45:00", temperature=82)
    sched = client.last_write("cool_schedule_settings")
    assert any(sp.secondsInDay == 45900 for sp in sched.setpoints.values())
    await call(hass, DOMAIN, "schedule_remove", f"climate.{T1}", schedule="cool", day="mon", time="12:45:00")
    assert all(sp.secondsInDay != 45900 for sp in client.last_write("cool_schedule_settings").setpoints.values())
    await call(hass, DOMAIN, "average_sensors", f"climate.{T3}",
               sensors=[sensor_entity(ROOM1_NAME, "temperature"), sensor_entity(ROOM2_NAME, "temperature")])
    assert len(client.last_write("remote_comfort_sensing_settings").multiSensorSettings.multiSensorGroup) == 2


async def test_invalid_values_are_reported_not_sent(hass, fake_client):
    from homeassistant.exceptions import ServiceValidationError

    _, client = await setup(hass, fake_client)
    with pytest.raises(ServiceValidationError):
        await call(hass, DOMAIN, "schedule_add", f"climate.{T1}", schedule="cool", day="mon", time="12:50:00", temperature=82)
    assert client.writes == []


# --- auth ---------------------------------------------------------------------------


async def test_rejected_credentials_at_setup(hass, fake_client):
    fake_client.reject = True
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER_ID, data={CONF_ISSUE_TOKEN: ISSUE, CONF_COOKIES: "SID=a"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())


async def test_auth_failure_while_running_starts_reauth(hass, fake_client):
    _, client = await setup(hass, fake_client)
    client.fail_auth()
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())
    assert hass.states.get(f"climate.{T1}").state == "unavailable"


async def test_rotated_cookies_are_saved(hass, fake_client):
    from custom_components.protonest.pynestproto.auth import GoogleCredentials

    entry, client = await setup(hass, fake_client)
    client.on_credentials_changed(GoogleCredentials(issue_token=ISSUE, cookies="SID=a; SIDCC=rotated"))
    assert entry.data[CONF_COOKIES] == "SID=a; SIDCC=rotated"



async def test_unresponsive_thermostat_goes_unavailable_except_online(hass, fake_client):
    _, client = await setup(hass, fake_client)
    client.set_responsive(THERMOSTAT1, False)
    await hass.async_block_till_done()
    assert hass.states.get(f"climate.{T1}").state == "unavailable"
    assert hass.states.get(f"switch.{T1}_sunblock").state == "unavailable"
    assert hass.states.get(f"binary_sensor.{T1}_online").state == "off"  # still available
    assert hass.states.get(f"climate.{T2}").state == "cool"  # others unaffected
    client.set_responsive(THERMOSTAT1, True)
    await hass.async_block_till_done()
    assert hass.states.get(f"climate.{T1}").state == "cool"
    assert hass.states.get(f"binary_sensor.{T1}_online").state == "on"


async def test_override_raises_a_notification(hass, fake_client):
    from homeassistant.components import persistent_notification

    _, client = await setup(hass, fake_client)
    client.override(THERMOSTAT1, "target_temperature_settings")
    await hass.async_block_till_done()
    notes = persistent_notification._async_get_or_create_notifications(hass)
    note = notes[f"{DOMAIN}_override_{THERMOSTAT1}_target_temperature_settings"]
    assert THERMOSTAT1_NAME in note["title"] and "temperature or mode" in note["message"]


async def test_set_temperature_with_a_mode_sets_that_modes_setpoint(hass, fake_client):
    _, client = await setup(hass, fake_client)
    await call(hass, "climate", "set_temperature", f"climate.{T1}", hvac_mode="heat", temperature=66)
    trait = client.last_write("target_temperature_settings")
    assert trait.targetTemperature.heatingTarget.value == pytest.approx(18.8889, abs=1e-3)
    assert hass.states.get(f"climate.{T1}").state == "heat"
    assert hass.states.get(f"climate.{T1}").attributes["temperature"] == 66


async def test_heat_cool_with_no_stored_setpoints_uses_defaults(hass, fake_client):
    _, client = await setup(hass, fake_client)
    rid = next(e.unique_id for e in er.async_get(hass).entities.values() if e.entity_id == f"climate.{T2}").split("-")[0]

    def forget(trait):
        trait.enabled.value = False
        trait.targetTemperature.heatingTarget.value = 0.0
        trait.targetTemperature.coolingTarget.value = 0.0

    await client.async_update(rid, "target_temperature_settings", forget)
    await call(hass, "climate", "set_hvac_mode", f"climate.{T2}", hvac_mode="heat_cool")
    tt = client.last_write("target_temperature_settings").targetTemperature
    assert (tt.heatingTarget.value, tt.coolingTarget.value) == (pytest.approx(20.0), pytest.approx(25.5))


async def test_pair_sensor_refuses_a_thermostats_own_sensor(hass, fake_client):
    from homeassistant.exceptions import ServiceValidationError

    _, client = await setup(hass, fake_client)
    assert hass.states.get(f"sensor.{T1}_temperature") is not None
    with pytest.raises(ServiceValidationError):
        await call(hass, DOMAIN, "pair_sensor", f"climate.{T3}", sensor=f"sensor.{T1}_temperature")
    assert client.writes == []


async def test_sensors_with_the_same_name_stay_distinct(hass, fake_client):
    _, client = await setup(hass, fake_client)
    room1, room2 = (next(s for s in client.cache.resource_types if client.cache.display_name(s) == n)
                     for n in (ROOM1_NAME, ROOM2_NAME))

    def rename(trait):
        trait.label = ROOM1_NAME

    await client.async_update(room2, "label", rename)
    await hass.async_block_till_done()
    options = hass.states.get(f"select.{T3}_target_sensor").attributes["options"]
    labels = [f"{ROOM1_NAME} ({room1[-4:]})", f"{ROOM1_NAME} ({room2[-4:]})"]
    assert all(label in options for label in labels)
    await call(hass, "select", "select_option", f"select.{T3}_target_sensor", option=labels[1])
    assert client.last_write("remote_comfort_sensing_settings").activeRcsSelection.activeRcsSensor.resourceId == room2
