"""Diagnostics include the state but never the credentials."""

import json

from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from tests.home import THERMOSTAT1_NAME, THERMOSTAT1_SERIAL, THERMOSTAT2_NAME, THERMOSTAT3_NAME

from .test_setup import setup


async def test_diagnostics_redacted(hass, hass_client, fake_client):
    entry, _ = await setup(hass, fake_client)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(diag)
    assert "SIDCC" not in text and "iframerpc" not in text  # credentials
    assert THERMOSTAT1_SERIAL not in text  # serial numbers
    assert diag["status"]["ready"] is True
    names = {d["name"] for d in diag["devices"].values()}
    assert {THERMOSTAT1_NAME, THERMOSTAT2_NAME, THERMOSTAT3_NAME} <= names
    mf = next(d for d in diag["devices"].values() if d["name"] == THERMOSTAT1_NAME)
    assert "target_temperature_settings" in mf["traits"]
