"""Numeric settings: eco and safety limits, filter reminder threshold."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant

from .coordinator import ProtoNestConfigEntry
from .entity import ProtoNestEntity, validation_errors
from .pynestproto.model import Thermostat, thermostats
from .pynestproto.write import FILTER_LABEL, THRESHOLDS, set_filter_threshold, set_threshold

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class ProtoNestNumberDescription(NumberEntityDescription):
    label: str
    value: Callable[[Thermostat], float | None]
    set: Callable[[float], Callable]


def _limit(key: str, name: str) -> ProtoNestNumberDescription:
    spec = THRESHOLDS[key]
    return ProtoNestNumberDescription(
        key=key, name=name, entity_category=EntityCategory.CONFIG, label=spec.label,
        device_class="temperature", native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=round((spec.min_f - 32) * 5 / 9, 1), native_max_value=round((spec.max_f - 32) * 5 / 9, 1),
        native_step=0.5, mode=NumberMode.BOX,
        # Shown only while the limit is on; turning it on/off is the matching switch.
        value=lambda t: t.threshold(key)[1] if t.threshold(key)[0] else None,
        set=lambda v: set_threshold(key, v),
    )


NUMBERS: tuple[ProtoNestNumberDescription, ...] = (
    _limit("eco_heat", "Eco heating limit"),
    _limit("eco_cool", "Eco cooling limit"),
    _limit("safety_low", "Safety low limit"),
    _limit("safety_high", "Safety high limit"),
    ProtoNestNumberDescription(
        key="filter_threshold", name="Filter reminder threshold", entity_category=EntityCategory.CONFIG,
        label=FILTER_LABEL, native_unit_of_measurement=UnitOfTime.HOURS, native_min_value=50,
        native_max_value=10000, native_step=10, mode=NumberMode.BOX,
        value=lambda t: t.filter_threshold_hours, set=set_filter_threshold,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ProtoNestConfigEntry, async_add_entities) -> None:
    c = entry.runtime_data
    async_add_entities(ProtoNestNumber(c, t, d) for t in thermostats(c.client.cache) for d in NUMBERS)


class ProtoNestNumber(ProtoNestEntity, NumberEntity):
    entity_description: ProtoNestNumberDescription

    def __init__(self, coordinator, thermostat, description) -> None:
        super().__init__(coordinator, thermostat, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value(self.device)

    @validation_errors
    async def async_set_native_value(self, value: float) -> None:
        d = self.entity_description
        await self.write(d.label, d.set(value))
