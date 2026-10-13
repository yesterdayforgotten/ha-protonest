"""Setup screen: paste parsing, validation, duplicates, reauth."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.protonest.const import CONF_COOKIES, CONF_ISSUE_TOKEN, DOMAIN
from custom_components.protonest.pynestproto.auth import (
    AuthUnavailable,
    CredentialsRejected,
    GoogleCredentials,
    NestToken,
)

ISSUE = "https://accounts.google.com/o/oauth2/iframerpc?action=issueToken&client_id=abc"
CURL = f"curl '{ISSUE}' \\\n  -H 'Accept: */*' \\\n  -H 'Cookie: SID=sid1; SIDCC=cc1'"


class FakeAuth:
    outcome: object = None

    def __init__(self, session, creds, on_cookies_rotated=None, **kw):
        self._creds = creds
        self._rotated = on_cookies_rotated

    @property
    def credentials(self):
        return self._creds

    async def async_get_token(self, min_validity=300):
        if isinstance(FakeAuth.outcome, Exception):
            raise FakeAuth.outcome
        # Google rotates a cookie during validation; the flow must store it.
        self._creds = GoogleCredentials(issue_token=self._creds.issue_token, cookies="SID=sid1; SIDCC=cc2")
        return NestToken(jwt="g.0.x", user_id=FakeAuth.outcome or "123456", expires_at=datetime(2100, 1, 1, tzinfo=UTC))


@pytest.fixture(autouse=True)
def fake_auth():
    FakeAuth.outcome = None
    with patch("custom_components.protonest.config_flow.NestAuth", FakeAuth), \
         patch("custom_components.protonest.async_setup_entry", return_value=True):
        yield


async def start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_paste_curl_creates_entry_with_rotated_cookies(hass):
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"capture": CURL})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ISSUE_TOKEN: ISSUE, CONF_COOKIES: "SID=sid1; SIDCC=cc2"}
    assert result["result"].unique_id == "123456"


@pytest.mark.parametrize(
    ("capture", "outcome", "error"),
    [
        ("not a capture", None, "bad_capture"),
        (CURL, CredentialsRejected("No active session found."), "rejected"),
        (CURL, AuthUnavailable("HTTP 503"), "cannot_connect"),
    ],
)
async def test_errors_shown_on_form(hass, capture, outcome, error):
    FakeAuth.outcome = outcome
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"capture": capture})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_same_account_twice_aborts(hass):
    MockConfigEntry(domain=DOMAIN, unique_id="123456", data={}).add_to_hass(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"capture": CURL})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth_updates_credentials(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="123456", data={CONF_ISSUE_TOKEN: ISSUE, CONF_COOKIES: "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"capture": CURL})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_COOKIES] == "SID=sid1; SIDCC=cc2"


async def test_reauth_with_another_account_is_refused(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="123456", data={CONF_ISSUE_TOKEN: ISSUE, CONF_COOKIES: "old"})
    entry.add_to_hass(hass)
    FakeAuth.outcome = "999999"
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"capture": CURL})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_account"
    assert entry.data[CONF_COOKIES] == "old"
