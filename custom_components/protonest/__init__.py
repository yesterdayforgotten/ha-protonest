"""ProtoNest: Nest thermostats over the home.nest.com protobuf API."""

from __future__ import annotations

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import CONF_COOKIES, PLATFORMS
from .coordinator import ProtoNestConfigEntry, ProtoNestCoordinator, credentials_from
from .pynestproto import codec
from .pynestproto.auth import CredentialsRejected, GoogleCredentials
from .pynestproto.client import NestClient


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry) -> bool:
    """Connect, wait for the first snapshot, then create entities."""
    await hass.async_add_executor_job(codec.load_trait_types)  # ~60 module imports

    @callback
    def save_rotated_cookies(creds: GoogleCredentials) -> None:
        # Google rotates cookies on every token request and soon rejects the
        # old ones, so the stored copy must always be the latest.
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_COOKIES: creds.cookies})

    # We send the cookie header ourselves; a real jar would add duplicates.
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
    client = NestClient(session, credentials_from(entry), on_credentials_changed=save_rotated_cookies)
    client.start()
    try:
        await client.async_wait_ready(timeout=45)
    except CredentialsRejected as err:
        await client.async_stop()
        raise ConfigEntryAuthFailed(str(err)) from err
    except TimeoutError as err:
        last = client.status.last_error
        await client.async_stop()
        raise ConfigEntryNotReady(f"no data from Nest yet{f': {last}' if last else ''}") from err

    coordinator = ProtoNestCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    # The coordinator (and with it the client) shuts down when the entry unloads.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
