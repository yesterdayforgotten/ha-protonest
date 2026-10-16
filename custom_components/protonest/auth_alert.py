"""Telling the user when Google stops accepting the saved sign-in.

Home Assistant shows a reauthentication prompt under Settings, which is easy
to miss. This adds a notification in Home Assistant and, if chosen in the
integration's options, a message through notify services (a phone, say).
"""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import CONF_NOTIFY, DOMAIN

_LOGGER = logging.getLogger(__name__)

TITLE = "ProtoNest: Nest sign-in needed"
MESSAGE = (
    "Google stopped accepting ProtoNest's saved sign-in, so your Nest devices are unavailable in "
    "Home Assistant until you sign in again.\n\n"
    "Go to **Settings → Devices & services**, choose **Reconfigure** on ProtoNest, and paste a new "
    "capture from a Firefox private window, as when you set it up."
)
PHONE_MESSAGE = (
    "Google stopped accepting ProtoNest's saved sign-in, so your Nest devices are unavailable in "
    "Home Assistant. Reconfigure ProtoNest under Settings → Devices & services."
)


def _notification_id(entry: ConfigEntry) -> str:
    return f"{DOMAIN}_auth_{entry.entry_id}"


@callback
def async_report_auth_failure(hass: HomeAssistant, entry: ConfigEntry) -> None:
    persistent_notification.async_create(hass, MESSAGE, title=TITLE, notification_id=_notification_id(entry))
    for service in entry.options.get(CONF_NOTIFY, []):
        hass.async_create_task(_send(hass, service), f"{DOMAIN} sign-in alert to notify.{service}")


@callback
def async_clear_auth_failure(hass: HomeAssistant, entry: ConfigEntry) -> None:
    persistent_notification.async_dismiss(hass, _notification_id(entry))


async def _send(hass: HomeAssistant, service: str) -> None:
    try:
        await hass.services.async_call("notify", service, {"title": TITLE, "message": PHONE_MESSAGE}, blocking=True)
    except Exception:
        _LOGGER.exception("Couldn't send the sign-in alert through notify.%s", service)
