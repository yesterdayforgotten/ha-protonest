"""A target sensor schedule run by Home Assistant.

Unlike the thermostat's own sensor schedule (four fixed periods, the same every
day), this one has any number of entries per day at any time. Each entry means
"from this time on, control to these sensors" until the next entry, carrying
over midnight and the week: the thermostat's own sensor, one paired sensor, or
two or more paired sensors averaged. At each entry ProtoNest sets the target
sensor (held), just like picking it in the Target sensor select.

A manual change holds until the next entry, as with the Nest app: an entry is
applied once, when it becomes due, not re-applied while it stays due.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .pynestproto.model import SENSOR_AVERAGE, Thermostat
from .pynestproto.write import THERMOSTAT_SENSOR, select_sensor, set_multi_sensor

_LOGGER = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_LABEL = "remote_comfort_sensing_settings"
_RETRY = timedelta(minutes=5)  # after a failed switch (thermostat offline, ...)
_STORAGE_VERSION = 1


@dataclass(frozen=True)
class Entry:
    day: str
    minute: int  # minutes since midnight, local time
    sensors: tuple[str, ...]  # (THERMOSTAT_SENSOR,), (sensor ID,), or 2+ sensor IDs to average

    @property
    def key(self) -> tuple[int, int]:
        return DAYS.index(self.day), self.minute

    @property
    def time(self) -> str:
        return f"{self.minute // 60:02d}:{self.minute % 60:02d}"

    def as_dict(self) -> dict[str, Any]:
        return {"day": self.day, "time": self.time, "sensors": list(self.sensors)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Entry:
        h, m = (int(x) for x in d["time"].split(":")[:2])
        return cls(d["day"], h * 60 + m, tuple(d["sensors"]))


def validate(entries: Iterable[Entry], paired: Iterable[str]) -> list[Entry]:
    """Check a schedule against the thermostat's paired sensors; return it sorted."""
    paired = set(paired)
    entries = list(entries)
    for e in entries:
        if e.day not in DAYS or not 0 <= e.minute < 24 * 60:
            raise ValueError(f"invalid day or time: {e.day} {e.time}")
    out = sorted(entries, key=lambda e: e.key)
    seen = set()
    for e in out:
        if e.key in seen:
            raise ValueError(f"two entries on {e.day} at {e.time}")
        seen.add(e.key)
        if not e.sensors or len(set(e.sensors)) != len(e.sensors):
            raise ValueError(f"{e.day} {e.time}: choose the thermostat, a sensor, or several sensors to average")
        if THERMOSTAT_SENSOR in e.sensors:
            if len(e.sensors) > 1:
                raise ValueError(f"{e.day} {e.time}: the thermostat's own sensor can't be averaged")
            continue
        missing = [s for s in e.sensors if s not in paired]
        if missing:
            raise ValueError(f"{e.day} {e.time}: not paired with this thermostat: {missing}")
    return out


def due_entry(entries: list[Entry], now: datetime) -> Entry | None:
    """The entry in effect at ``now`` (local): the latest one at or before it, wrapping the week."""
    if not entries:
        return None
    now_key = (now.weekday(), now.hour * 60 + now.minute)
    earlier = [e for e in entries if e.key <= now_key]
    return earlier[-1] if earlier else entries[-1]


def next_change(entries: list[Entry], now: datetime) -> datetime | None:
    """When the next entry starts, after ``now`` (an aware local datetime)."""
    if not entries:
        return None
    for offset in range(8):
        date = (now + timedelta(days=offset)).date()
        for e in entries:
            if e.day != DAYS[date.weekday()]:
                continue
            at = datetime.combine(date, time(e.minute // 60, e.minute % 60), tzinfo=now.tzinfo)
            if at > now:
                return at
    return None


def is_applied(thermostat: Thermostat, sensors: tuple[str, ...]) -> bool:
    """Whether the thermostat already controls to these sensors (held)."""
    active = thermostat.active_sensor
    if len(sensors) == 1:
        return active == sensors[0]
    return active == SENSOR_AVERAGE and set(thermostat.average_sensors) == set(sensors)


def mutation(sensors: tuple[str, ...]):
    return select_sensor(sensors[0]) if len(sensors) == 1 else set_multi_sensor(list(sensors))


@dataclass
class _Plan:
    enabled: bool = False
    entries: list[Entry] | None = None
    applied: Entry | None = None  # the last entry switched to, so it isn't re-applied

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "entries": [e.as_dict() for e in self.entries or []],
                "applied": self.applied.as_dict() if self.applied else None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _Plan:
        applied = d.get("applied")
        return cls(bool(d.get("enabled")), [Entry.from_dict(e) for e in d.get("entries", [])],
                   Entry.from_dict(applied) if applied else None)


class SensorScheduler:
    """Runs every thermostat's Home Assistant sensor schedule for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        thermostat: Callable[[str], Thermostat | None],
        write: Callable[[str, str, Any], Any],
        on_change: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, _STORAGE_VERSION, f"{DOMAIN}.sensor_schedule.{entry_id}")
        self._thermostat = thermostat
        self._write = write
        self._on_change = on_change
        self._plans: dict[str, _Plan] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._running = False

    # --- state ------------------------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._plans = {rid: _Plan.from_dict(p) for rid, p in data.get("thermostats", {}).items()}

    async def _async_save(self) -> None:
        # Saved right away (changes are rare), so a reload straight after an edit can't lose it.
        await self._store.async_save({"thermostats": {rid: p.as_dict() for rid, p in self._plans.items()}})

    def enabled(self, rid: str) -> bool:
        return rid in self._plans and self._plans[rid].enabled

    def entries(self, rid: str) -> list[Entry]:
        return list(self._plans[rid].entries or []) if rid in self._plans else []

    async def async_set_entries(self, rid: str, entries: list[Entry]) -> None:
        plan = self._plans.setdefault(rid, _Plan())
        plan.entries = entries
        await self._async_save()
        self._on_change()
        await self._async_run(rid)

    async def async_set_enabled(self, rid: str, enabled: bool) -> None:
        plan = self._plans.setdefault(rid, _Plan())
        plan.enabled = enabled
        plan.applied = None  # turning it on applies the current entry right away
        await self._async_save()
        self._on_change()
        await self._async_run(rid)

    # --- running ----------------------------------------------------------------------

    async def async_start(self) -> None:
        self._running = True
        for rid in list(self._plans):
            await self._async_run(rid)

    @callback
    def stop(self) -> None:
        self._running = False
        while self._timers:
            self._timers.popitem()[1]()

    async def async_remove(self) -> None:
        await self._store.async_remove()

    async def _async_run(self, rid: str) -> None:
        """Apply the due entry if it hasn't been yet, then wait for the next one."""
        if not self._running:
            return
        async with self._locks.setdefault(rid, asyncio.Lock()):
            if (cancel := self._timers.pop(rid, None)) is not None:
                cancel()
            plan = self._plans.get(rid)
            if plan is None or not plan.enabled or not plan.entries:
                return
            now = dt_util.now()
            due = due_entry(plan.entries, now)
            wake = next_change(plan.entries, now)
            if due is not None and due != plan.applied:
                if not await self._async_apply(rid, due):
                    wake = min(w for w in (wake, now + _RETRY) if w is not None)
            if wake is not None and self._running:
                self._timers[rid] = async_track_point_in_time(self._hass, self._wake(rid), wake)

    def _wake(self, rid: str):
        async def wake(_now: datetime) -> None:
            self._timers.pop(rid, None)
            await self._async_run(rid)

        return wake

    async def _async_apply(self, rid: str, entry: Entry) -> bool:
        thermostat = self._thermostat(rid)
        if thermostat is None:
            return False
        try:
            if not is_applied(thermostat, entry.sensors):
                await self._write(rid, _LABEL, mutation(entry.sensors))
        except (ServiceValidationError, ValueError) as err:
            # The entry itself can't be applied (say, a sensor was unpaired since):
            # retrying won't help, so skip it until the next one.
            _LOGGER.warning("Sensor schedule for %s: skipped %s %s: %s", thermostat.name, entry.day, entry.time, err)
        except HomeAssistantError as err:
            _LOGGER.warning("Sensor schedule for %s: couldn't switch for %s %s (retrying in %s): %s",
                            thermostat.name, entry.day, entry.time, _RETRY, err)
            return False
        self._plans[rid].applied = entry
        await self._async_save()
        return True
