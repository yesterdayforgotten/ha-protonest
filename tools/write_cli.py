"""Change a thermostat setting, with a dry run by default.

    .venv/bin/python tools/write_cli.py Hallway --cool 74F           # dry run
    .venv/bin/python tools/write_cli.py Hallway --cool 74F --send    # really send
    .venv/bin/python tools/write_cli.py Hallway --mode off --send
    .venv/bin/python tools/write_cli.py Hallway --cool 74F --replay captures/observe-....bin
    .venv/bin/python tools/write_cli.py Hallway --fan-run 15 --fan-hours 9-23 --send
    .venv/bin/python tools/write_cli.py Hallway --fan-on 30m --send   (or --fan-off)
    .venv/bin/python tools/write_cli.py Hallway --humidity 45 --send
    .venv/bin/python tools/write_cli.py Hallway --sensor thermostat --send  (or a sensor name, or schedule)
    .venv/bin/python tools/write_cli.py Hallway --eco on --send

Reads current state from the Observe stream (or a recording with --replay),
builds the update by read-modify-write, and shows the before/after diff and
how the request compares to a captured web-app write. With --send (and a
confirmation), sends it and watches the stream for the device to confirm.
Temperatures need a unit: 74F or 23.5C. Durations: 15m, 2h, 1d, 1w.
Each group of options (temperature/mode, fan, humidity, sensor, eco) changes a
different trait, so only one group can be used per command.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

import aiohttp
from google.protobuf import json_format
from google.protobuf import message as pb_message

import credfile
from observe_cli import flatten
from pynestproto import codec
from pynestproto.auth import AuthError
from pynestproto.observe import ACCEPTED, CONFIRMED, TraitCache, build_observe_request
from pynestproto.proto_gen.nestlabs.gateway import v1_pb2
from pynestproto.transport import OBSERVE_PATH, NestTransport, TransportError
from pynestproto.write import (
    HVAC_MODES,
    NestWriter,
    PreparedUpdate,
    FAN_RUN_TIMES,
    HUMIDITY_MAX,
    HUMIDITY_MIN,
    EARLY_ON_LABEL,
    DISPLAY_LABEL,
    SENSOR_SCHEDULE,
    SETTING_FLAGS,
    THERMOSTAT_SENSOR,
    THRESHOLDS,
    WriteError,
    check_target_temperature,
    prepare_update,
    clear_multi_sensor,
    pair_sensor,
    run_fan,
    select_sensor,
    DISPLAY_FARSIGHT,
    FILTER_LABEL,
    set_filter_changed,
    set_filter_threshold,
    EQUIPMENT_LABEL,
    DAYS,
    SCHEDULE_LABELS,
    add_schedule_entry,
    remove_schedule_entry,
    update_schedule_entry,
    set_farsight,
    set_humidifier,
    set_humidity_control,
    set_early_on,
    set_eco,
    set_fan_run_duration,
    set_fan_schedule,
    set_flag,
    set_hvac_mode,
    set_multi_sensor,
    set_sensor_schedule,
    set_target_humidity,
    set_target_temperature,
    set_temperature_scale,
    set_threshold,
    stop_fan,
    unpair_sensor,
)

LABEL = "target_temperature_settings"
FAN_LABEL = "fan_control_settings"
HUMIDITY_LABEL = "humidity_control_settings"
SENSOR_LABEL = "remote_comfort_sensing_settings"
ECO_LABEL = "eco_mode_state"
GOLDEN = credfile.ROOT / "old" / "sdk-deobfuscated" / "heat_update_request.bin"
THERMOSTAT = "nest.resource.NestLearningThermostat3Resource"
SENSOR_RESOURCE = "nest.resource.NestKryptoniteResource"


def parse_temp(text: str) -> float:
    """'74F' or '23.5C' -> Celsius. A unit is required to avoid mix-ups."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*°?\s*([CcFf])\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"{text!r}: give a unit, e.g. 74F or 23.5C")
    value, unit = float(m.group(1)), m.group(2).upper()
    return (value - 32) * 5 / 9 if unit == "F" else value


def temp_or_off(text: str):
    return "off" if text.strip().lower() == "off" else parse_temp(text)


def parse_duration(text: str) -> int:
    """'15m', '2h', '1d', '1w' -> seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", text.lower())
    if not m:
        raise argparse.ArgumentTypeError(f"{text!r}: use e.g. 15m, 2h, 1d, 1w")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


def parse_hours(text: str) -> tuple[int, int]:
    """'9-23' -> (9, 23); 'all' -> (0, 0), which the thermostat treats as all day."""
    if text.strip().lower() == "all":
        return 0, 0
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"{text!r}: use START-END in whole hours, e.g. 9-23, or 'all'")
    return int(m.group(1)), int(m.group(2))


def field_paths(msg: pb_message.Message, prefix: str = "") -> set[str]:
    """Paths of every field present in a message (values ignored)."""
    out: set[str] = set()
    for field, value in msg.ListFields():
        path = f"{prefix}{field.name}"
        out.add(path)
        if field.message_type is not None and not field.is_repeated:
            if field.message_type.full_name == "google.protobuf.Any":
                inner = codec.unpack(value)
                if inner is not None:
                    out |= field_paths(inner, path + ">")
            else:
                out |= field_paths(value, path + ".")
    return out


def print_prepared(prepared: PreparedUpdate, cache: TraitCache) -> None:
    name = cache.display_name(prepared.resource_id)
    print(f"Device : {name} ({prepared.resource_id})")
    print(f"Trait  : {prepared.label}")
    old = flatten(json_format.MessageToDict(prepared.old, always_print_fields_with_no_presence=True))
    new = flatten(json_format.MessageToDict(prepared.new, always_print_fields_with_no_presence=True))
    print("Change :")
    for path in sorted(old.keys() | new.keys()):
        if old.get(path) != new.get(path):
            print(f"  ~ {path}: {old.get(path)} -> {new.get(path)}")
    print(f"Request: {len(prepared.request.SerializeToString())} bytes, whole trait sent (read-modify-write)")

    if prepared.label == LABEL and GOLDEN.exists():
        golden = v1_pb2.BatchUpdateStateRequest.FromString(GOLDEN.read_bytes())
        ours_p, gold_p = field_paths(prepared.request), field_paths(golden)
        # Known difference: the web app fills originalActorInfo's insides; the
        # server stores (and we send back) it empty. See tests/test_write.py.
        known = {p for p in gold_p - ours_p if ".originalActorInfo." in p}
        extra, missing = ours_p - gold_p, gold_p - ours_p - known
        if not extra and not missing:
            print("Layout : same fields as the captured web-app write (except the known originalActorInfo detail)")
        else:
            print("Layout : DIFFERS from the captured web-app write")
            for p in sorted(extra):
                print(f"  + only ours: {p}")
            for p in sorted(missing):
                print(f"  - only web app: {p}")


class Session:
    """Observe stream feeding a cache, with change notifications."""

    def __init__(self, transport: NestTransport) -> None:
        self.transport = transport
        self.cache = TraitCache()
        self.last_frame = 0.0
        self.frames = 0
        self.changes: asyncio.Queue = asyncio.Queue()
        self.error: BaseException | None = None

    async def run(self) -> None:
        try:
            async for raw in self.transport.async_stream(OBSERVE_PATH, build_observe_request()):
                self.frames += 1
                self.last_frame = time.monotonic()
                for change in self.cache.apply_frame(codec.ObserveFrame.FromString(raw)):
                    self.changes.put_nowait((time.monotonic(), change))
        except Exception as err:  # surfaced to the main flow
            self.error = err

    async def settle(self, quiet: float = 1.5, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error:
                raise self.error
            if self.frames and time.monotonic() - self.last_frame >= quiet:
                while not self.changes.empty():  # discard the initial snapshot
                    self.changes.get_nowait()
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("no state received from the Observe stream")


def find_device(cache: TraitCache, wanted: str) -> str:
    thermostats = [rid for rid, t in cache.resource_types.items() if t == THERMOSTAT]
    for rid in thermostats:
        if wanted in (rid, cache.display_name(rid)) or wanted.lower() == cache.display_name(rid).lower():
            return rid
    names = ", ".join(cache.display_name(r) for r in thermostats)
    raise SystemExit(f"No thermostat {wanted!r}. Known: {names}")


def changes(cache: TraitCache, rid: str, args: argparse.Namespace) -> list[tuple[str, object]]:
    """(trait label, mutation) for every option given, in a fixed order."""
    out: list[tuple[str, object]] = []
    user = cache.user_resource_id()

    if args.mode or args.heat is not None or args.cool is not None:
        if user is None:
            print("Note: user resource not seen; the change won't name you as the originator.")
        if args.mode:
            out.append((LABEL, set_hvac_mode(args.mode, user=user)))
        if args.heat is not None or args.cool is not None:
            out.append((LABEL, set_target_temperature(heat=args.heat, cool=args.cool, user=user)))

    schedule = {}
    if args.fan_schedule:
        schedule["enabled"] = args.fan_schedule == "on"
    if args.fan_run:
        schedule["run_seconds_per_hour"] = args.fan_run * 60
    if args.fan_hours:
        schedule["start_hour"], schedule["end_hour"] = args.fan_hours
    if schedule:
        out.append((FAN_LABEL, set_fan_schedule(**schedule)))
    if args.fan_duration:
        out.append((FAN_LABEL, set_fan_run_duration(args.fan_duration)))
    if args.fan_on:
        out.append((FAN_LABEL, run_fan(None if args.fan_on == "remembered" else parse_duration(args.fan_on))))
    if args.fan_off:
        out.append((FAN_LABEL, stop_fan()))

    if args.humidity is not None:
        caps = cache.latest(rid, "hvac_equipment_capabilities")
        if not caps or not (caps.message.hasHumidifier or caps.message.hasDehumidifier):
            raise ValueError("this thermostat has no humidifier or dehumidifier; target humidity would do nothing")
        out.append((HUMIDITY_LABEL, set_target_humidity(args.humidity)))
    if args.sensor is not None:
        out.append((SENSOR_LABEL, select_sensor(resolve_sensor(cache, rid, args.sensor))))
    if args.pair_sensor:
        out.append((SENSOR_LABEL, pair_sensor(find_sensor(cache, args.pair_sensor))))
    if args.unpair_sensor:
        out.append((SENSOR_LABEL, unpair_sensor(find_sensor(cache, args.unpair_sensor))))
    if args.eco is not None:
        out.append((ECO_LABEL, set_eco(args.eco == "on", user=user)))

    for name, (label, _) in SETTING_FLAGS.items():
        value = getattr(args, name)
        if value:
            out.append((label, set_flag(name, value == "on")))
    if args.early_on is not None:
        out.append((EARLY_ON_LABEL, set_early_on(None if args.early_on == "off" else int(args.early_on))))
    for name, spec in THRESHOLDS.items():
        value = getattr(args, name)
        if value is not None:
            out.append((spec.label, set_threshold(name, None if value == "off" else value)))
    if args.units:
        out.append((DISPLAY_LABEL, set_temperature_scale(args.units)))

    if args.humidifier_type or args.humidifier_fan:
        out.append((EQUIPMENT_LABEL, set_humidifier(
            kind=args.humidifier_type,
            run_fan=None if args.humidifier_fan is None else args.humidifier_fan == "on",
        )))
    if args.humidity_control:
        out.append((HUMIDITY_LABEL, set_humidity_control(args.humidity_control == "on")))
    if args.filter_changed:
        if args.filter_changed == "over-6-months":
            out.append((FILTER_LABEL, set_filter_changed(None)))
        else:
            from datetime import UTC, datetime as dt
            out.append((FILTER_LABEL, set_filter_changed(dt.fromisoformat(args.filter_changed).replace(tzinfo=UTC))))
    if args.filter_threshold:
        out.append((FILTER_LABEL, set_filter_threshold(args.filter_threshold)))
    if args.farsight:
        out.append((DISPLAY_LABEL, set_farsight(args.farsight)))

    for spec, op in ((args.schedule_add, "add"), (args.schedule_update, "update"), (args.schedule_remove, "remove")):
        if spec:
            out.append(schedule_change(op, spec, user))

    # Experimental
    if args.multi_sensor:
        ids = [find_sensor(cache, name.strip()) for name in args.multi_sensor.split(",")]
        out.append((SENSOR_LABEL, set_multi_sensor(ids, set_source=not args.multi_sensor_keep_source)))
    if args.clear_multi_sensor:
        out.append((SENSOR_LABEL, clear_multi_sensor()))
    if args.sensor_schedule:
        choices = {}
        for part in args.sensor_schedule.split(","):
            period, _, who = part.partition("=")
            who = who.strip()
            choices[period.strip()] = THERMOSTAT_SENSOR if who.lower() == THERMOSTAT_SENSOR else find_sensor(cache, who)
        out.append((SENSOR_LABEL, set_sensor_schedule(choices, standard_times=args.sensor_schedule_standard)))
    elif args.sensor_schedule_standard:
        out.append((SENSOR_LABEL, set_sensor_schedule({}, standard_times=True)))
    return out


def build(cache: TraitCache, args: argparse.Namespace) -> PreparedUpdate:
    rid = find_device(cache, args.device)
    found = changes(cache, rid, args)
    labels = sorted({label for label, _ in found})
    if len(labels) != 1:
        raise ValueError(f"these options change different traits ({', '.join(labels)}); run them separately")

    def mutate(trait):  # one write per trait, e.g. mode and setpoints together
        for _, m in found:
            m(trait)

    prepared = prepare_update(cache, rid, labels[0], mutate)
    if labels[0] == LABEL:
        check_target_temperature(prepared.new)
    return prepared


def resolve_sensor(cache: TraitCache, rid: str, wanted: str) -> str:
    if wanted.lower() in (THERMOSTAT_SENSOR, SENSOR_SCHEDULE):
        return wanted.lower()
    rcs = cache.latest(rid, SENSOR_LABEL)
    paired = [s.deviceId.resourceId for s in rcs.message.associatedRcsSensors] if rcs and rcs.message else []
    for sid in paired:
        if wanted in (sid, cache.display_name(sid)) or wanted.lower() == cache.display_name(sid).lower():
            return sid
    names = ", ".join(["thermostat", "schedule", *(cache.display_name(s) for s in paired)])
    raise ValueError(f"no sensor {wanted!r} for {cache.display_name(rid)}. Choices: {names}")


def parse_clock(text: str) -> int:
    hh, _, mm = text.partition(":")
    return int(hh) * 3600 + int(mm or 0) * 60


def schedule_change(op: str, spec: str, user: str | None):
    """'cool mon 12:45 82F' (add), 'cool mon 12:45 time=18:15 day=tue temp=70F' (update),
    'cool mon 12:45' (remove). Range entries take 'heat=68F cool=76F'."""
    kind, day, clock, *rest = spec.split()
    if kind not in SCHEDULE_LABELS:
        raise ValueError(f"schedule must be one of {list(SCHEDULE_LABELS)}")
    seconds = parse_clock(clock)
    opts = dict(r.split("=", 1) for r in rest if "=" in r)
    temps = [parse_temp(r) for r in rest if "=" not in r]
    heat = parse_temp(opts["heat"]) if "heat" in opts else (temps[0] if kind == "heat" and temps else None)
    cool = parse_temp(opts["cool"]) if "cool" in opts else (temps[0] if kind == "cool" and temps else None)
    if "temp" in opts:
        heat, cool = (parse_temp(opts["temp"]), None) if kind == "heat" else (None, parse_temp(opts["temp"]))
    label = SCHEDULE_LABELS[kind]
    if op == "add":
        return label, add_schedule_entry(kind, day, seconds, heat=heat, cool=cool, user=user)
    if op == "remove":
        return label, remove_schedule_entry(kind, day, seconds)
    return label, update_schedule_entry(
        kind, day, seconds, new_day=opts.get("day"),
        new_seconds=parse_clock(opts["time"]) if "time" in opts else None,
        heat=heat, cool=cool, user=user)


def show_schedule(cache: TraitCache, rid: str, kind: str) -> None:
    v = cache.latest(rid, SCHEDULE_LABELS[kind])
    if not v or v.message is None:
        print(f"no {kind} schedule")
        return
    names = {e: d for d, e in DAYS.items()}
    order = list(DAYS.values())
    entries = sorted(v.message.setpoints.items(), key=lambda kv: (order.index(kv[1].dayOfWeek), kv[1].secondsInDay))
    print(f"{cache.display_name(rid)} {kind} schedule ({len(entries)} entries):")
    for key, sp in entries:
        temps = []
        if kind in ("heat", "range"):
            temps.append(f"heat {sp.heatingTarget.value * 9 / 5 + 32:.1f}F")
        if kind in ("cool", "range"):
            temps.append(f"cool {sp.coolingTarget.value * 9 / 5 + 32:.1f}F")
        print(f"  [{key:3}] {names.get(sp.dayOfWeek, '?')} {sp.secondsInDay // 3600:02d}:{sp.secondsInDay % 3600 // 60:02d}  {'  '.join(temps)}")


def find_sensor(cache: TraitCache, wanted: str) -> str:
    """Any remote sensor in the home, by name or resource ID."""
    sensors = [rid for rid, t in cache.resource_types.items() if t == SENSOR_RESOURCE]
    for sid in sensors:
        if wanted in (sid, cache.display_name(sid)) or wanted.lower() == cache.display_name(sid).lower():
            return sid
    raise ValueError(f"no sensor {wanted!r}. Known: {', '.join(cache.display_name(s) for s in sensors)}")


async def watch(session: Session, prepared: PreparedUpdate, sent_at: float, timeout: float = 20) -> None:
    deadline = sent_at + timeout
    while (left := deadline - time.monotonic()) > 0:
        try:
            at, change = await asyncio.wait_for(session.changes.get(), left)
        except TimeoutError:
            break
        if (change.resource_id, change.label) != (prepared.resource_id, prepared.label):
            continue
        kind = "CONFIRMED" if change.state_type == CONFIRMED else "ACCEPTED"
        same = change.new.message == prepared.new
        print(f"  {kind:9} after {at - sent_at:4.1f}s  {'matches what we sent' if same else 'DIFFERS from what we sent'}")
        if not same:
            sent = flatten(json_format.MessageToDict(prepared.new, always_print_fields_with_no_presence=True))
            got = flatten(json_format.MessageToDict(change.new.message, always_print_fields_with_no_presence=True))
            for path in sorted(sent.keys() | got.keys()):
                if sent.get(path) != got.get(path):
                    print(f"      {path}: sent {sent.get(path)}, got {got.get(path)}")
        if change.state_type == CONFIRMED:
            return
    print("  no CONFIRMED state within 20s; check the Nest app")


async def run_live(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as http:
        transport = NestTransport(http, credfile.make_auth(http))
        session = Session(transport)
        task = asyncio.create_task(session.run())
        try:
            await session.settle()
            if args.schedule_show:
                show_schedule(session.cache, find_device(session.cache, args.device), args.schedule_show)
                return 0
            prepared = build(session.cache, args)
            print_prepared(prepared, session.cache)
            if not prepared.changed:
                print("\nNothing to change: the thermostat already has these settings.")
                return 0
            if not args.send:
                print("\nDry run: nothing sent. Add --send to apply.")
                return 0
            if not args.yes:
                answer = await asyncio.to_thread(input, "\nSend this to the thermostat? [y/N] ")
                if answer.strip().lower() != "y":
                    print("Not sent.")
                    return 0
            writer = NestWriter(transport, session.cache)
            sent_at = time.monotonic()
            op = await writer.async_send(prepared)
            progress = v1_pb2.TraitOperation.State.Name(op.progress) if op else "skipped"
            print(f"\nSent. Server reports: {progress}")
            await watch(session, prepared, sent_at)
            return 0
        except (AuthError, TransportError, WriteError, codec.StreamError) as err:
            print(f"Failed: {type(err).__name__}: {err}")
            return 1
        finally:
            task.cancel()


def run_replay(args: argparse.Namespace) -> int:
    cache = TraitCache()
    for raw in codec.parse_stream_body(Path(args.replay).read_bytes()):
        cache.apply_frame(codec.ObserveFrame.FromString(raw))
    if args.schedule_show:
        show_schedule(cache, find_device(cache, args.device), args.schedule_show)
        return 0
    prepared = build(cache, args)
    print(f"(state from recording {args.replay})")
    print_prepared(prepared, cache)
    print("\nReplay: nothing sent." if prepared.changed else "\nNothing to change.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("device", help="thermostat name (e.g. 'Hallway') or resource ID")
    parser.add_argument("--mode", choices=HVAC_MODES)
    parser.add_argument("--heat", type=parse_temp, help="heating setpoint, e.g. 68F")
    parser.add_argument("--cool", type=parse_temp, help="cooling setpoint, e.g. 76F")
    fan = parser.add_argument_group("fan")
    fan.add_argument("--fan-schedule", choices=("on", "off"), help="periodic fan schedule")
    fan.add_argument("--fan-run", type=int, choices=[t // 60 for t in FAN_RUN_TIMES],
                     help="schedule run time, minutes per hour")
    fan.add_argument("--fan-hours", type=parse_hours, help="schedule hours START-END (0-23), or 'all'")
    fan.add_argument("--fan-on", nargs="?", const="remembered", metavar="DURATION",
                     help="run the fan now (default: the remembered length)")
    fan.add_argument("--fan-off", action="store_true", help="stop a manual fan run")
    fan.add_argument("--fan-duration", type=parse_duration, help="set the remembered run length")
    other = parser.add_argument_group("humidity, target sensor, eco")
    other.add_argument("--humidity", type=int, metavar="PERCENT",
                       help=f"target humidity {HUMIDITY_MIN}-{HUMIDITY_MAX}%% in steps of 5")
    other.add_argument("--sensor", metavar="NAME", help="'thermostat', 'schedule', or a paired sensor's name")
    other.add_argument("--eco", choices=("on", "off"))
    other.add_argument("--pair-sensor", metavar="NAME", help="pair a remote sensor with this thermostat")
    other.add_argument("--unpair-sensor", metavar="NAME", help="unpair a remote sensor (not while in use)")
    settings = parser.add_argument_group("settings")
    for name in SETTING_FLAGS:
        settings.add_argument(f"--{name.replace('_', '-')}", dest=name, choices=("on", "off"))
    settings.add_argument("--early-on", choices=("off", "0", "1", "2", "3", "4", "5"), help="off, or 0-5 hours")
    for name, spec in THRESHOLDS.items():
        settings.add_argument(f"--{name.replace('_', '-')}", dest=name, type=temp_or_off, metavar="TEMP|off",
                              help=f"{spec.min_f:g}-{spec.max_f:g}F, or off")
    settings.add_argument("--units", choices=("C", "F"), help="units on the thermostat's own display")
    settings.add_argument("--humidifier-type", choices=("bypass", "steam"),
                          help="bypass = humidify only with heat; steam = always")
    settings.add_argument("--humidifier-fan", choices=("on", "off"), help="run the fan while humidifying")
    settings.add_argument("--humidity-control", choices=("on", "off"))
    settings.add_argument("--filter-changed", metavar="DATE",
                          help="when the filter was last changed: YYYY-MM-DD, or over-6-months")
    settings.add_argument("--filter-threshold", type=float, metavar="HOURS",
                          help="runtime hours before the reminder (not in the app; default 500)")
    settings.add_argument("--farsight", choices=sorted(DISPLAY_FARSIGHT), help="what the screen shows from afar")
    sched = parser.add_argument_group("weekly schedule")
    sched.add_argument("--schedule-show", choices=list(SCHEDULE_LABELS), help="print a schedule and exit")
    sched.add_argument("--schedule-add", metavar="SPEC", help="'cool mon 12:45 82F'")
    sched.add_argument("--schedule-update", metavar="SPEC", help="'cool mon 12:45 time=18:15 temp=70F'")
    sched.add_argument("--schedule-remove", metavar="SPEC", help="'cool mon 12:45'")
    exp = parser.add_argument_group("experimental (not in the Nest app)")
    exp.add_argument("--multi-sensor", metavar="NAMES", help="average these paired sensors, comma-separated")
    exp.add_argument("--clear-multi-sensor", action="store_true", help="remove the multi-sensor group")
    exp.add_argument("--multi-sensor-keep-source", action="store_true",
                     help="with --multi-sensor: enable the group but keep controlling to the current sensor")
    exp.add_argument("--sensor-schedule-standard", action="store_true",
                     help="reset the sensor schedule to the app's standard period times (7/11/16/21)")
    exp.add_argument("--sensor-schedule", metavar="PERIODS",
                     help="sensor per fixed period, e.g. 'morning=Living Room,night=thermostat' (morning/midday/evening/night)")
    parser.add_argument("--send", action="store_true", help="really send (asks to confirm)")
    parser.add_argument("--yes", action="store_true", help="with --send: don't ask")
    parser.add_argument("--replay", help="build from a recording instead of the live stream (never sends)")
    args = parser.parse_args()
    if not any(v not in (None, False) for k, v in vars(args).items() if k not in ("device", "send", "yes", "replay")):
        parser.error("nothing to change; see --help for the options")
    if args.fan_on and args.fan_off:
        parser.error("--fan-on and --fan-off together make no sense")
    if args.replay and args.send:
        parser.error("--replay never sends")
    codec.load_trait_types()
    try:
        if args.replay:
            return run_replay(args)
        return asyncio.run(run_live(args))
    except ValueError as err:
        print(f"Refused: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
