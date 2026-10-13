"""Fan: run the HVAC fan now (for the remembered duration) or stop it."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant

from .coordinator import ProtoNestConfigEntry
from .entity import ProtoNestEntity, validation_errors
from .pynestproto.model import thermostats
from .pynestproto.write import run_fan, stop_fan

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(ProtoNestFan(c, t) for t in thermostats(c.client.cache) if t.has_fan)


class ProtoNestFan(ProtoNestEntity, FanEntity):
    _attr_name = "Fan"
    _attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF

    def __init__(self, coordinator, thermostat) -> None:
        super().__init__(coordinator, thermostat, "fan")

    @property
    def is_on(self) -> bool:
        """On while a manual run is active (the schedule's runs are on the binary sensor)."""
        return self.device.fan_timer_end is not None

    @validation_errors
    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        await self.write("fan_control_settings", run_fan(None if self.device.fan_run_duration else 3600))

    @validation_errors
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.write("fan_control_settings", stop_fan())
