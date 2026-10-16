"""Setup and reauthentication: paste the iframerpc request copied from Firefox."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import CONF_COOKIES, CONF_ISSUE_TOKEN, CONF_NOTIFY, DOMAIN
from .pynestproto.auth import AuthUnavailable, CredentialsRejected, NestAuth, parse_capture

_LOGGER = logging.getLogger(__name__)

CONF_CAPTURE = "capture"
SCHEMA = vol.Schema({vol.Required(CONF_CAPTURE): TextSelector(TextSelectorConfig(multiline=True))})


class ProtoNestConfigFlow(ConfigFlow, domain=DOMAIN):
    """One field: Firefox's "Copy as cURL" of the iframerpc request (or the
    issue token URL and the cookies on two lines)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ProtoNestOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"problem": ""}
        if user_input is not None:
            data, user_id, error, detail = await self._validate(user_input[CONF_CAPTURE])
            if data is not None:
                await self.async_set_unique_id(user_id)
                if self.source == "reauth":
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"Nest account {user_id}", data=data)
            errors["base"] = error
            placeholders["problem"] = detail
        return self.async_show_form(
            step_id="user", data_schema=SCHEMA, errors=errors, description_placeholders=placeholders
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_user()

    async def _validate(self, text: str):
        """Returns (data, user_id, error_key, detail)."""
        try:
            creds = parse_capture(text)
        except ValueError as err:
            return None, None, "bad_capture", str(err)
        session = async_create_clientsession(self.hass, cookie_jar=aiohttp.DummyCookieJar())
        rotated = []
        auth = NestAuth(session, creds, on_cookies_rotated=rotated.append)
        try:
            token = await auth.async_get_token()
        except CredentialsRejected as err:
            return None, None, "rejected", str(err)
        except AuthUnavailable as err:
            return None, None, "cannot_connect", str(err)
        final = auth.credentials  # includes cookies Google rotated just now
        return {CONF_ISSUE_TOKEN: final.issue_token, CONF_COOKIES: final.cookies}, token.user_id, None, ""


# Notify services that aren't a destination of their own.
_NOT_TARGETS = {"persistent_notification", "send_message", "notify"}


class ProtoNestOptionsFlow(OptionsFlow):
    """Where else to send the alert when Google stops accepting the sign-in."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_NOTIFY, [])
        services = set(self.hass.services.async_services_for_domain("notify")) - _NOT_TARGETS
        schema = vol.Schema({
            vol.Optional(CONF_NOTIFY, default=current): SelectSelector(SelectSelectorConfig(
                options=sorted(services | set(current)), multiple=True, custom_value=True,
                mode=SelectSelectorMode.DROPDOWN,
            )),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
