"""Stream live Nest state and print what changes.

    .venv/bin/python tools/observe_cli.py              # snapshot, then live changes
    .venv/bin/python tools/observe_cli.py --record     # also save frames to captures/
    .venv/bin/python tools/observe_cli.py --all-traits # subscribe to every trait in the schema

Prints a snapshot of thermostats and sensors once the initial burst settles,
then one block per change with a field-level diff, stamped with the time the
change happened (from monotonicVersion). Ctrl+C prints connection statistics.

Uses .credentials.json (see save_credentials.py) and keeps it up to date as
Google rotates cookies.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from google.protobuf import descriptor_pool, json_format

import credfile
from pynestproto import codec
from pynestproto.auth import AuthUnavailable, CredentialsRejected
from pynestproto.observe import ACCEPTED, CONFIRMED, TraitCache, TraitChange, build_observe_request
from pynestproto.proto_gen.google.rpc import stream_body_pb2
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.transport import (
    OBSERVE_PATH,
    OBSERVE_READ_TIMEOUT,
    AuthExpired,
    NestTransport,
    TransportError,
)

CAPTURE_DIR = credfile.ROOT / "captures"
SETTLE_SECONDS = 2.0

THERMOSTAT = "nest.resource.NestLearningThermostat3Resource"
SENSOR = "nest.resource.NestKryptoniteResource"


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def c_to_f(c: float) -> str:
    return f"{c:.1f}°C/{c * 9 / 5 + 32:.0f}°F"


def kind(state_type: int) -> str:
    return "CONFIRMED" if state_type == CONFIRMED else "ACCEPTED "


def flatten(obj, prefix: str = "") -> dict[str, object]:
    if isinstance(obj, dict):
        out: dict[str, object] = {}
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
        return out
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        out = {}
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
        return out
    return {prefix: obj}


def diff(change: TraitChange) -> list[str]:
    if change.new.message is None:
        return [f"  (type {change.new.type_name} not in schema)"]
    # Print false/zero scalars too, so "True -> False" shows as a change rather
    # than the field vanishing (proto3 omits default values).
    as_dict = lambda m: json_format.MessageToDict(m, always_print_fields_with_no_presence=True)  # noqa: E731
    new = flatten(as_dict(change.new.message))
    old = flatten(as_dict(change.old.message)) if change.old and change.old.message else {}
    lines = []
    for path in sorted(old.keys() | new.keys()):
        if path not in new:
            lines.append(f"  - {path}: {old[path]}   (cleared)")
        elif path not in old:
            lines.append(f"  + {path}: {new[path]}")
        elif old[path] != new[path]:
            lines.append(f"  ~ {path}: {old[path]} -> {new[path]}")
    return lines or ["  (no field-level difference)"]


class Observer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cache = TraitCache()
        self.settled = False
        self.last_frame = 0.0
        self.frames = 0
        self.noops: list[float] = []
        self.streams: list[tuple[float, str]] = []  # (duration, end reason)
        self.record = None
        if args.record:
            CAPTURE_DIR.mkdir(exist_ok=True)
            path = CAPTURE_DIR / f"observe-{datetime.now():%Y%m%d-%H%M%S}.bin"
            self.record = path.open("ab")
            print(f"Recording frames to {path}")

    # --- output -------------------------------------------------------------

    def name(self, rid: str) -> str:
        return self.cache.display_name(rid)

    def snapshot(self) -> None:
        c = self.cache
        get = lambda rid, label: c.get(rid, label) or c.get(rid, label, ACCEPTED)  # noqa: E731
        print(f"\n[{now()}] ===== snapshot: {len(c.resource_types)} resources =====")
        for rid, rtype in sorted(c.resource_types.items(), key=lambda kv: self.name(kv[0])):
            if rtype == THERMOSTAT:
                self.print_thermostat(rid, get)
        for rid, rtype in sorted(c.resource_types.items(), key=lambda kv: self.name(kv[0])):
            if rtype == SENSOR:
                temp = get(rid, "current_temperature")
                batt = get(rid, "battery")
                t = c_to_f(temp.temperatureValue.temperature.value) if temp else "?"
                b = f"{batt.assessedVoltage.value:.2f} V" if batt and batt.HasField("assessedVoltage") else "?"
                print(f"  sensor  {self.name(rid):16} {t:16} battery {b}")
        print(f"[{now()}] ===== watching for changes (Ctrl+C to stop) =====\n")

    def print_thermostat(self, rid: str, get) -> None:
        SP = hvac_pb2.SetPointScheduleSettingsTrait
        print(f"  thermostat {self.name(rid)}  ({rid})")
        target = get(rid, "target_temperature_settings")
        if target:
            tt = target.targetTemperature
            mode = "OFF" if not target.enabled.value else SP.SetPointType.Name(tt.setpointType).removeprefix("SET_POINT_TYPE_")
            targets = []
            if mode in ("HEAT", "RANGE"):
                targets.append(f"heat to {c_to_f(tt.heatingTarget.value)}")
            if mode in ("COOL", "RANGE"):
                targets.append(f"cool to {c_to_f(tt.coolingTarget.value)}")
            print(f"    mode {mode:6} {'  '.join(targets)}")
        temp, hum = get(rid, "current_temperature"), get(rid, "current_humidity")
        if temp:
            h = f"{hum.humidityValue.humidity.value:.0f}%" if hum else "?"
            print(f"    now  {c_to_f(temp.temperatureValue.temperature.value)}  humidity {h}")
        hcs = get(rid, "humidity_control_settings")
        if hcs:
            print(f"    target humidity {hcs.targetHumidity.value:.0f}%")
        hvac = get(rid, "hvac_control")
        if hvac:
            active = [f.name for f, v in hvac.hvacState.ListFields() if v]
            print(f"    running: {', '.join(active) or 'idle'}")
        fan = get(rid, "fan_control_settings")
        if fan:
            F = hvac_pb2.FanControlSettingsTrait
            timer = fan.timerEnd.ToDatetime() if fan.HasField("timerEnd") and fan.timerEnd.seconds else None
            print(f"    fan  {F.FanMode.Name(fan.mode)}  timer_end {timer or '-'}")
        eco = get(rid, "eco_mode_state")
        if eco:
            print(f"    eco  {hvac_pb2.EcoModeStateTrait.EcoMode.Name(eco.ecoMode)}")
        rcs = get(rid, "remote_comfort_sensing_settings")
        if rcs:
            R = hvac_pb2.RemoteComfortSensingSettingsTrait
            sel = rcs.activeRcsSelection
            src = R.RcsSourceType.Name(sel.rcsSourceType).removeprefix("RCS_SOURCE_TYPE_")
            active = self.name(sel.activeRcsSensor.resourceId) if sel.HasField("activeRcsSensor") else "-"
            paired = [self.name(s.deviceId.resourceId) for s in rcs.associatedRcsSensors]
            print(f"    target sensor {src} {active}  (paired: {', '.join(paired) or 'none'})")

    def print_change(self, change: TraitChange) -> None:
        tag = "NEW TRAIT" if change.is_new else kind(change.state_type)
        # monotonicVersion is a microsecond Unix timestamp of the change.
        when = datetime.fromtimestamp(change.new.version / 1e6).strftime("%H:%M:%S.%f")[:-3]
        print(f"[{when}] {self.name(change.resource_id)} / {change.label}  [{tag}]")
        for line in diff(change):
            print(line)

    # --- stream handling ----------------------------------------------------

    def on_noop(self) -> None:
        t = time.monotonic()
        if self.args.verbose:
            gap = f" (+{t - self.noops[-1]:.0f}s)" if self.noops and self.noops[-1] is not None else " (first on this stream)"
            print(f"[{now()}] keepalive{gap}")
        self.noops.append(t)

    def handle(self, raw: bytes) -> None:
        self.frames += 1
        self.last_frame = time.monotonic()
        if self.record:
            self.record.write(stream_body_pb2.StreamBody(message=[raw]).SerializeToString())
            self.record.flush()
        for change in self.cache.apply_frame(codec.ObserveFrame.FromString(raw)):
            if self.settled or not change.is_new:
                self.print_change(change)

    async def settle_watch(self) -> None:
        while not self.settled:
            await asyncio.sleep(0.5)
            if self.frames and time.monotonic() - self.last_frame >= SETTLE_SECONDS:
                self.settled = True
                self.snapshot()

    async def run(self) -> int:
        def on_rotated(creds):
            if self.args.verbose:
                print(f"[{now()}] Google rotated cookies; saved")

        if self.args.all_traits:
            trait_types = sorted(
                name for name in _all_message_names() if name.endswith("Trait")
            )
        else:
            trait_types = None
        request = build_observe_request(trait_types)
        print(f"Subscribing to {len(request.traitTypeParams)} trait types")

        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            auth = credfile.make_auth(session, on_rotated=on_rotated)
            transport = NestTransport(session, auth)
            watcher = asyncio.create_task(self.settle_watch())
            delay = 2
            while True:
                started = time.monotonic()
                try:
                    token = await auth.async_get_token()
                    print(f"[{now()}] connecting (token valid until {token.expires_at.astimezone():%H:%M:%S})")
                    async for raw in transport.async_stream(
                        OBSERVE_PATH, request, read_timeout=self.args.read_timeout, on_noop=self.on_noop
                    ):
                        self.handle(raw)
                        delay = 2
                    reason = "server closed the stream"
                except CredentialsRejected as err:
                    print(f"[{now()}] credentials rejected: {err}")
                    print("Capture fresh credentials and run save_credentials.py.")
                    watcher.cancel()
                    return 1
                except (AuthExpired, AuthUnavailable, TransportError, codec.StreamError) as err:
                    reason = f"{type(err).__name__}: {err}"
                lasted = time.monotonic() - started
                self.streams.append((lasted, reason))
                print(f"[{now()}] stream ended after {lasted / 60:.1f} min: {reason}; reconnecting in {delay}s")
                self.noops.append(None)  # don't measure keepalive gaps across reconnects
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def stats(self) -> None:
        print(f"\n--- {self.frames} frames, {sum(n is not None for n in self.noops)} keepalives")
        gaps = [b - a for a, b in zip(self.noops, self.noops[1:]) if a is not None and b is not None]
        if gaps:
            print(f"    keepalive interval: min {min(gaps):.0f}s, max {max(gaps):.0f}s, mean {sum(gaps) / len(gaps):.0f}s")
        for lasted, reason in self.streams:
            print(f"    stream lasted {lasted / 60:.1f} min, ended: {reason}")
        if self.record:
            self.record.close()


def _all_message_names() -> list[str]:
    pool = descriptor_pool.Default()
    names = []
    for module in sys.modules.values():
        desc = getattr(module, "DESCRIPTOR", None)
        if getattr(module, "__name__", "").startswith("pynestproto.proto_gen") and desc is not None:
            names += [m.full_name for m in desc.message_types_by_name.values()]
    return [n for n in names if pool.FindMessageTypeByName(n)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--record", action="store_true", help="save raw frames under captures/")
    parser.add_argument("--all-traits", action="store_true", help="subscribe to every trait type in the schema")
    parser.add_argument(
        "--read-timeout", type=float, default=OBSERVE_READ_TIMEOUT,
        help="reconnect after this many seconds of silence (keepalives come every 600 s)",
    )
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="log keepalives and cookie rotation")
    args = parser.parse_args()

    if not credfile.DEFAULT_PATH.exists():
        print("No .credentials.json; run tools/save_credentials.py first.")
        return 1
    codec.load_trait_types()
    observer = Observer(args)
    loop = asyncio.new_event_loop()
    task = loop.create_task(observer.run())
    loop.add_signal_handler(signal.SIGINT, task.cancel)
    if args.duration:
        loop.call_later(args.duration, task.cancel)
    try:
        return loop.run_until_complete(task) or 0
    except asyncio.CancelledError:
        return 0
    finally:
        observer.stats()
        with contextlib.suppress(Exception):
            loop.close()


if __name__ == "__main__":
    sys.exit(main())
