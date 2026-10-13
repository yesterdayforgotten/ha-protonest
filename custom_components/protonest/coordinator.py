"""Bridges the push-based NestClient into Home Assistant."""

from __future__ import annotations

import logging
from collections.abc import Callable

from google.protobuf import message as pb_message
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_COOKIES, CONF_ISSUE_TOKEN, DOMAIN
from .pynestproto.auth import AuthError, GoogleCredentials
from .pynestproto.client import NestClient
from .pynestproto.observe import TraitCache
from .pynestproto.transport import TransportError
from .pynestproto.write import WriteError

_LOGGER = logging.getLogger(__name__)

type ProtoNestConfigEntry = ConfigEntry[ProtoNestCoordinator]


class ProtoNestCoordinator(DataUpdateCoordinator[TraitCache]):
    """No polling: the client pushes changes, which are forwarded to entities."""

    def __init__(self, hass: HomeAssistant, entry: ProtoNestConfigEntry, client: NestClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.client = client
        self.data = client.cache
        self._unsubs: list[Callable[[], None]] = [
            client.add_listener(self._on_changes),
            client.add_auth_failure_listener(self._on_auth_failure),
            client.add_override_listener(self._on_override),
        ]

    @callback
    def _on_changes(self, _changes) -> None:
        self.async_set_updated_data(self.client.cache)

    @callback
    def _on_auth_failure(self) -> None:
        self.config_entry.async_start_reauth(self.hass)
        self.async_update_listeners()  # entities go unavailable

    @callback
    def _on_override(self, resource_id: str, label: str) -> None:
        name = self.client.cache.display_name(resource_id)
        what = _SETTING_NAMES.get(label, label.replace("_", " "))
        persistent_notification.async_create(
            self.hass,
            f"{name} didn't keep the change to its {what} made from Home Assistant: the thermostat "
            "replaced it with its own value (for example one set on the device while it was offline). "
            "Check the setting and change it again if needed.",
            title=f"{name}: change replaced",
            notification_id=f"{DOMAIN}_override_{resource_id}_{label}",
        )

    async def _async_update_data(self) -> TraitCache:
        # Never polled; a manual refresh just republishes the live cache.
        return self.client.cache

    @property
    def connected(self) -> bool:
        return self.client.status.connected

    async def async_write(
        self,
        resource_id: str,
        label: str,
        mutate: Callable[[pb_message.Message], None],
        check: Callable[[pb_message.Message], None] | None = None,
    ) -> None:
        """Send one trait update; turns failures into errors HA shows the user."""
        try:
            await self.client.async_update(resource_id, label, mutate, check)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except (WriteError, TransportError, AuthError) as err:
            raise HomeAssistantError(f"Nest rejected the change: {err}") from err

    async def async_shutdown(self) -> None:
        # Safe to call twice: HA shuts the coordinator down when the entry unloads.
        while self._unsubs:
            self._unsubs.pop()()
        await self.client.async_stop()
        await super().async_shutdown()


def credentials_from(entry: ConfigEntry) -> GoogleCredentials:
    return GoogleCredentials(issue_token=entry.data[CONF_ISSUE_TOKEN], cookies=entry.data[CONF_COOKIES])


_SETTING_NAMES = {
    "target_temperature_settings": "temperature or mode",
    "eco_mode_state": "Eco setting",
    "fan_control_settings": "fan setting",
    "humidity_control_settings": "humidity setting",
    "remote_comfort_sensing_settings": "target sensor",
}
