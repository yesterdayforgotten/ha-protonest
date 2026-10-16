"""Shared entity base."""

from __future__ import annotations

import functools

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ProtoNestCoordinator
from .pynestproto.model import RemoteSensor, Structure, Thermostat

_MODELS = {Thermostat: "Nest Learning Thermostat", RemoteSensor: "Nest Temperature Sensor", Structure: "Nest home"}


class ProtoNestEntity(CoordinatorEntity[ProtoNestCoordinator]):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ProtoNestCoordinator, device: Thermostat | RemoteSensor | Structure, key: str
    ) -> None:
        super().__init__(coordinator)
        self.resource_id = device.resource_id
        self._kind = type(device)
        self._is_thermostat = self._kind is Thermostat
        self._attr_unique_id = f"{device.resource_id}-{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.resource_id)},
            name=device.name,
            manufacturer="Google Nest",
            model=device.model or _MODELS[self._kind],
            model_id=device.model_id if device.model_id != device.model else None,
            serial_number=device.serial_number,
            sw_version=device.firmware,
        )

    @property
    def device(self) -> Thermostat:
        return self._kind(self.coordinator.client.cache, self.resource_id)

    @property
    def available(self) -> bool:
        client = self.coordinator.client
        # Stay available across the brief planned reconnects. Unavailable: a
        # rejected login, a device leaving the account, or a thermostat that
        # isn't responding (a change left unconfirmed, or Nest says offline).
        return self._connected and (not self._is_thermostat or client.responsive(self.resource_id))

    @property
    def _connected(self) -> bool:
        client = self.coordinator.client
        return client.ready and not client.status.auth_failed and self.resource_id in client.cache.resource_types

    @property
    def user(self) -> str | None:
        return self.coordinator.client.cache.user_resource_id()

    async def write(self, label: str, mutate, check=None) -> None:
        await self.coordinator.async_write(self.resource_id, label, mutate, check)


def validation_errors(func):
    """Report invalid values (raised as ValueError while building a change)
    as HA validation errors, so the user sees the reason."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    return wrapper
