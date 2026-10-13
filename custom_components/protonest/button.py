"""Filter replaced: records today as the filter change date."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import ProtoNestConfigEntry
from .entity import ProtoNestEntity, validation_errors
from .pynestproto.model import thermostats
from .pynestproto.write import FILTER_LABEL, set_filter_changed

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(FilterReplacedButton(c, t) for t in thermostats(c.client.cache))


class FilterReplacedButton(ProtoNestEntity, ButtonEntity):
    """One-way: the thermostat recalculates its runtime from this date, and the
    previous count can't be restored afterwards."""

    _attr_name = "Filter replaced"
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator, thermostat) -> None:
        super().__init__(coordinator, thermostat, "filter_replaced")

    @validation_errors
    async def async_press(self) -> None:
        # The local date (stored at 00:00 UTC, like the app's month picks).
        today = datetime.combine(dt_util.now().date(), datetime.min.time(), UTC)
        await self.write(FILTER_LABEL, set_filter_changed(today))
