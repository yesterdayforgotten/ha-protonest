"""The home device (Home/Away, HVAC alerts), newer thermostat
sensors, and alerts when Google stops accepting the sign-in."""

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import ServiceCall
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.protonest.const import CONF_COOKIES, CONF_ISSUE_TOKEN, CONF_NOTIFY, DOMAIN
from custom_components.protonest.pynestproto.model import structures
from tests.home import USER_ID
from tests_ha.test_setup import ISSUE, T1, call, setup


def home_entity(hass, client, platform: str, key: str) -> str:
    home = structures(client.cache)[0]
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{home.resource_id}-{key}")
    assert entity_id is not None, key
    return entity_id


async def test_home_entities_show_the_snapshot(hass, fake_client):
    _, client = await setup(hass, fake_client)
    home = structures(client.cache)[0]
    assert hass.states.get(home_entity(hass, client, "sensor", "mode")).state == home.mode
    assert hass.states.get(home_entity(hass, client, "switch", "hvac_alerts")).state == "on"
    assert hass.states.get(home_entity(hass, client, "sensor", "last_hvac_alert")) is not None
    assert hass.states.get(f"sensor.{T1}_seasonal_savings").state == "finished"


async def test_hvac_alerts_switch_writes_the_home(hass, fake_client):
    _, client = await setup(hass, fake_client)
    switch = home_entity(hass, client, "switch", "hvac_alerts")
    await call(hass, "switch", "turn_off", switch)
    rid, label, trait = client.writes[-1]
    assert rid.startswith("STRUCTURE_") and label == "hvac_diagnostics_alerts_settings"
    assert trait.enableAlerts.value is False
    assert hass.states.get(switch).state == "off"


async def test_a_server_error_is_reported_as_a_failed_change(hass, fake_client):
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.protonest.pynestproto.codec import StreamError

    _, client = await setup(hass, fake_client)

    async def unavailable(*args, **kwargs):
        raise StreamError(14, "no healthy upstream")

    client.async_update = unavailable
    with pytest.raises(HomeAssistantError, match="no healthy upstream"):
        await call(hass, "switch", "turn_off", home_entity(hass, client, "switch", "hvac_alerts"))


async def test_sign_in_failure_notifies_here_and_on_chosen_services(hass, fake_client):
    sent: list[ServiceCall] = []
    hass.services.async_register("notify", "mobile_app_phone", sent.append)
    entry, client = await setup(hass, fake_client)
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY: ["mobile_app_phone"]})
    client.fail_auth()
    await hass.async_block_till_done()
    notes = persistent_notification._async_get_or_create_notifications(hass)
    assert "sign-in" in notes[f"{DOMAIN}_auth_{entry.entry_id}"]["title"]
    assert len(sent) == 1 and "Reconfigure ProtoNest" in sent[0].data["message"]


async def test_sign_in_rejected_at_startup_notifies_and_clears_once_fixed(hass, fake_client):
    fake_client.reject = True
    entry = MockConfigEntry(domain=DOMAIN, unique_id=USER_ID, data={CONF_ISSUE_TOKEN: ISSUE, CONF_COOKIES: "SID=a"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    note_id = f"{DOMAIN}_auth_{entry.entry_id}"
    assert note_id in persistent_notification._async_get_or_create_notifications(hass)
    fake_client.reject = False
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert note_id not in persistent_notification._async_get_or_create_notifications(hass)


async def test_options_offer_notify_services(hass, fake_client):
    hass.services.async_register("notify", "mobile_app_phone", lambda call: None)
    entry, _ = await setup(hass, fake_client)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    options = result["data_schema"].schema[CONF_NOTIFY].config["options"]
    assert "mobile_app_phone" in options and "persistent_notification" not in options
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NOTIFY: ["mobile_app_phone"]})
    assert entry.options == {CONF_NOTIFY: ["mobile_app_phone"]}


async def test_heating_and_cooling_follow_the_equipment(hass, fake_client):
    from custom_components.protonest.pynestproto.model import Thermostat, thermostats

    _, client = await setup(hass, fake_client)
    t = Thermostat(client.cache, thermostats(client.cache)[0].resource_id)
    entity = er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, f"{t.resource_id}-cooling")
    assert hass.states.get(entity).state == ("on" if t.hvac_action == "cooling" else "off")
