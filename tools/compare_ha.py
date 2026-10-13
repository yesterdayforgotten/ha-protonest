"""Compare every ProtoNest entity in a running Home Assistant with a fresh
snapshot taken directly from the thermostats (using the dev tools' own
credentials, independent of HA).

    .venv/bin/python tools/compare_ha.py [--ha http://localhost:8123] [--config config]

Needs a long-lived access token in .ha_token (gitignored). Readings that drift
(temperatures, humidity) are compared with a small tolerance; everything else
must match exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

import credfile
from pynestproto import codec, model
from pynestproto.client import NestClient
from pynestproto.write import FAN_TIMER_DURATIONS, SENSOR_PERIODS, THERMOSTAT_SENSOR

ROOT = credfile.ROOT
HVAC = {"off": "off", "heat": "heat", "cool": "cool", "range": "heat_cool"}
DURATIONS = {900: "15 min", 1800: "30 min", 2700: "45 min", 3600: "1 hour", 7200: "2 hours", 14400: "4 hours",
             28800: "8 hours", 43200: "12 hours", 86400: "1 day", 259200: "3 days", 432000: "5 days", 604800: "1 week"}
FARSIGHT = {"target": "Target temperature", "current": "Current temperature", "digital_clock": "Digital clock",
            "analog_clock": "Analog clock", "weather": "Weather", "none": "Nothing"}
HUMIDIFIER = {"bypass": "Only with heat", "steam": "Any time"}


def f(c):
    return None if c is None else c * 9 / 5 + 32


def onoff(v):
    return None if v is None else ("on" if v else "off")


def ha_get(base: str, token: str, path: str):
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


async def snapshot() -> model.TraitCache:
    codec.load_trait_types()
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        auth = credfile.make_auth(session)
        client = NestClient(session, auth.credentials, auth=auth)
        client.start()
        await client.async_wait_ready(timeout=60)
        await client.async_stop()
        return client.cache


def expected(cache, rid: str, key: str):
    """(expected value, tolerance or None, attribute checks) for one entity."""
    is_t = cache.resource_types.get(rid) in model.THERMOSTAT_TYPES
    t = model.Thermostat(cache, rid) if is_t else model.RemoteSensor(cache, rid)
    name = lambda sid: "Thermostat" if sid == THERMOSTAT_SENSOR else cache.display_name(sid)  # noqa: E731
    if not is_t:
        return {"temperature": (f(t.temperature), 0.2), "battery_voltage": (t.battery_voltage, 0.02)}.get(key, (None, None))
    if key == "climate":
        heat, cool = t.target_heat, t.target_cool
        if t.eco_active:
            heat = t.threshold("eco_heat")[1] if t.threshold("eco_heat")[0] else None
            cool = t.threshold("eco_cool")[1] if t.threshold("eco_cool")[0] else None
        # HA shows a °F climate entity's current temperature in whole degrees.
        attrs = {"current_temperature": (f(t.current_temperature), 0.51),
                 "current_humidity": (round(t.current_humidity), 1),
                 "preset_mode": ("eco" if t.eco_active else "none", None)}
        if t.hvac_mode in ("heat", "cool"):
            attrs["temperature"] = (round(f(heat if t.hvac_mode == "heat" else cool)), 0)
        if t.hvac_mode == "range":
            attrs["target_temp_low"] = (round(f(heat)), 0)
            attrs["target_temp_high"] = (round(f(cool)), 0)
        if t.has_humidifier:
            attrs["humidity"] = (t.target_humidity if t.humidity_control_enabled else None, 0.5)
        return HVAC.get(t.hvac_mode), None, attrs
    table = {
        "fan": (onoff(t.fan_timer_end is not None), None),
        "fan_schedule": (onoff(t.fan_schedule_enabled), None),
        "humidity_control": (onoff(t.humidity_control_enabled), None),
        "humidifier_fan": (onoff(t.humidifier_runs_fan), None),
        "target_sensor": ({"schedule": "Schedule", "average": "Average of all sensors"}.get(t.active_sensor) or name(t.active_sensor), None),
        "fan_schedule_run_time": (f"{t.fan_schedule_minutes} min per hour", None),
        "fan_schedule_start": (f"{t.fan_schedule_hours[0]:02d}:00", None),
        "fan_schedule_end": (f"{t.fan_schedule_hours[1]:02d}:00", None),
        "fan_run_duration": (DURATIONS.get(t.fan_run_duration), None),
        "early_on": ("Off" if t.early_on_hours is None else f"{t.early_on_hours} h", None),
        "farsight": (FARSIGHT.get(t.farsight), None),
        "display_units": (f"°{t.temperature_scale}", None),
        "humidifier_type": (HUMIDIFIER.get(t.humidifier_type), None),
        "filter_threshold": (t.filter_threshold_hours, 0.01),
        "humidity": (t.current_humidity, 0.6),
        "temperature": (f(t.current_temperature), 0.2),
        "backplate_temperature": (f(t.backplate_temperature), 0.2),
        "filter_runtime": (t.filter_runtime_hours, 0.5),
        "filter_reminder_level": (t.filter_reminder_level, None),
        "filter_changed": (t.filter_changed, None),
        "running": (", ".join(s.removesuffix("Active") for s in t.active_stages) or "idle", None),
        "battery_voltage": (t.battery_voltage, 0.02),
        "filter_replacement_needed": (onoff(t.filter_replacement_needed), None),
        "leaf": (onoff(t.leaf), None),
        "fan_running": (onoff(t.fan_running), None),
        "online": (onoff(t.online), None),
        "time_to_target": (round(t.time_to_target / 60) if t.time_to_target else None, 1),
        "compressor_lockout": (t.compressor_lockout_until, None),
    }
    for flag in ("auto_schedule", "home_away_assist", "cool_to_dry", "sunblock", "airwave", "wake_on_approach", "filter_reminder"):
        table[flag] = (onoff(t.flag(flag)), None)
    for lim in ("eco_heat", "eco_cool", "safety_low", "safety_high"):
        on, val = t.threshold(lim)
        table[f"{lim}_enabled"] = (onoff(on), None)
        table[lim] = (f(val) if on else None, 0.15)
    for period in SENSOR_PERIODS:
        table[f"sensor_{period}"] = (name(t.sensor_for_period(period)), None)
    return table.get(key, (None, None))


def matches(want, got, tol) -> bool:
    if want is None:
        return got in (None, "unknown", "unavailable")
    if isinstance(want, datetime):
        try:
            return abs((datetime.fromisoformat(got) - want).total_seconds()) < 2
        except (TypeError, ValueError):
            return False
    if tol is not None:
        try:
            return abs(float(got) - float(want)) <= tol
        except (TypeError, ValueError):
            return False
    return str(got) == str(want)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ha", default="http://localhost:8123")
    ap.add_argument("--config", type=Path, default=ROOT / "config")
    args = ap.parse_args()
    token = (ROOT / ".ha_token").read_text().strip()

    registry = json.loads((args.config / ".storage" / "core.entity_registry").read_text())
    entities = {e["entity_id"]: e["unique_id"] for e in registry["data"]["entities"] if e["platform"] == "protonest"}
    states = {s["entity_id"]: s for s in ha_get(args.ha, token, "/api/states") if s["entity_id"] in entities}
    cache = asyncio.run(snapshot())

    checked, problems, skipped = 0, [], []
    for eid in sorted(entities):
        rid, key = entities[eid].split("-", 1)
        st = states.get(eid)
        if st is None:
            problems.append(f"{eid}: missing from HA")
            continue
        exp = expected(cache, rid, key)
        if key == "filter_replaced":
            skipped.append(eid)  # a button's state is its last press time
            continue
        want, tol, attrs = (exp + (None,))[:3] if len(exp) == 2 else exp
        if want is None and tol is None and key not in ("filter_changed", "compressor_lockout", "time_to_target", "humidifier_fan", "humidifier_type"):
            if exp == (None, None):
                skipped.append(eid)
                continue
        checked += 1
        if not matches(want, st["state"], tol):
            problems.append(f"{eid}: HA shows {st['state']!r}, thermostat says {want!r}")
        for attr, (aw, atol) in (attrs or {}).items():
            if not matches(aw, st["attributes"].get(attr), atol):
                problems.append(f"{eid}.{attr}: HA shows {st['attributes'].get(attr)!r}, thermostat says {aw!r}")

    print(f"Compared {checked} entities ({len(entities)} registered; {len(skipped)} not comparable: {', '.join(skipped) or '-'})")
    if problems:
        print(f"{len(problems)} mismatch(es):")
        for p in problems:
            print("  " + p)
        return 1
    print("Every entity matches the thermostats.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
