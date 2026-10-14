"""Connection manager: keeps the Observe stream alive and sends writes.

One long-running task reads the Observe stream into a TraitCache and tells
listeners what changed. Observed server behavior it is built around:

- every stream is ended by the server 60 minutes after it opened, so the
  client reconnects a few minutes before that, with a token that is valid at
  connect time (the token is only checked at connect);
- a reconnect starts with a full snapshot, which the cache diffs against what
  it already has, so nothing is lost across reconnects;
- rejected Google credentials need a new browser capture: the client stops and
  reports it instead of retrying.

Import-heavy setup (codec.load_trait_types) must be done before start(), and in
Home Assistant from an executor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
from google.protobuf import message as pb_message

from . import codec
from .auth import AuthUnavailable, CredentialsRejected, GoogleCredentials, NestAuth
from .model import Device
from .observe import CONFIRMED, TraitCache, TraitChange, build_observe_request
from .proto_gen.nestlabs.gateway import v1_pb2
from .transport import OBSERVE_PATH, STREAM_MAX_AGE, AuthExpired, NestTransport, TransportError
from .write import NestWriter, prepare_update

_LOGGER = logging.getLogger(__name__)

RECONNECT_MARGIN = 5 * 60  # reconnect this long before the server's 60-minute cutoff
# A thermostat that hasn't confirmed a change after this long, and has
# confirmed nothing since it was sent, is treated as not responding. Nest's own
# online status only changes after 68 minutes of silence.
UNRESPONSIVE_AFTER = 120
# After a change is confirmed, a different value arriving this soon for the same
# setting means the thermostat replaced it (e.g. with a local change it made
# while offline).
OVERRIDE_WINDOW = 30
SETTLE_SECONDS = 1.5  # the initial snapshot is complete after this much quiet
_BACKOFF = (2, 5, 15, 30, 60, 120, 300)

ChangeListener = Callable[[list[TraitChange]], None]
OverrideListener = Callable[[str, str], None]  # (resource_id, trait label)


@dataclass
class _Sent:
    message: pb_message.Message
    at: float  # when sent (pending) or confirmed (recent), time.monotonic()


@dataclass
class ConnectionStatus:
    connected: bool = False
    last_error: str | None = None
    auth_failed: bool = False


class NestClient:
    """Live Nest state plus writes, independent of Home Assistant."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: GoogleCredentials,
        on_credentials_changed: Callable[[GoogleCredentials], None] | None = None,
        *,
        stream_max_age: float = STREAM_MAX_AGE - RECONNECT_MARGIN,
        auth: NestAuth | None = None,
        transport: NestTransport | None = None,
        unresponsive_after: float = UNRESPONSIVE_AFTER,
    ) -> None:
        self.auth = auth or NestAuth(session, credentials, on_cookies_rotated=on_credentials_changed)
        self._transport = transport or NestTransport(session, self.auth)
        self.cache = TraitCache()
        self._writer = NestWriter(self._transport, self.cache)
        self._request = build_observe_request()
        self._stream_max_age = stream_max_age
        self._listeners: list[ChangeListener] = []
        self._auth_listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._settle_timer: asyncio.TimerHandle | None = None
        self._frames_this_stream = 0
        self.status = ConnectionStatus()
        self._unresponsive_after = unresponsive_after
        # Writes not yet confirmed, oldest first: quick writes to one trait each get confirmed in turn.
        self._pending: dict[tuple[str, str], list[_Sent]] = {}
        self._recent: dict[tuple[str, str], _Sent] = {}
        self._last_confirmed: dict[str, float] = {}
        self._override_listeners: list[OverrideListener] = []
        self._timers: list[asyncio.TimerHandle] = []
        self._auth_failed_event = asyncio.Event()
        # Writes replace a whole trait, built from the newest value we know. Until
        # the stream shows the last write, the newest value is the one we sent.
        # The stream may first show an earlier write of ours (its ACCEPTED or
        # CONFIRMED state), which is older: only a value that is none of ours (a
        # change made elsewhere) means the stream has moved on. Per trait:
        # (values that are ours: the one first built on, then each sent, last sent).
        self._write_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._unechoed: dict[tuple[str, str], tuple[list[pb_message.Message], pb_message.Message]] = {}

    # --- listeners ---------------------------------------------------------------

    def add_listener(self, listener: ChangeListener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def add_override_listener(self, listener: OverrideListener) -> Callable[[], None]:
        """Called when a thermostat replaced a change sent from here with its own value."""
        self._override_listeners.append(listener)
        return lambda: self._override_listeners.remove(listener) if listener in self._override_listeners else None

    def responsive(self, resource_id: str) -> bool:
        """False if the device left a change unconfirmed too long, or Nest reports it offline."""
        now = time.monotonic()
        last = self._last_confirmed.get(resource_id, float("-inf"))
        for (rid, _), sents in self._pending.items():
            sent = sents[0]
            if rid == resource_id and now - sent.at >= self._unresponsive_after and last < sent.at:
                return False
        return Device(self.cache, resource_id).online is not False

    def add_auth_failure_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._auth_listeners.append(listener)
        return lambda: self._auth_listeners.remove(listener) if listener in self._auth_listeners else None

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="protonest-observe")

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    async def async_wait_ready(self, timeout: float = 30) -> None:
        """Wait for the first complete snapshot.

        Raises CredentialsRejected if the credentials don't work, or
        TimeoutError if no state arrives in time.
        """
        waits = [asyncio.ensure_future(e.wait()) for e in (self._ready, self._auth_failed_event)]
        try:
            async with asyncio.timeout(timeout):
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
        if not self._ready.is_set():
            raise CredentialsRejected(self.status.last_error or "credentials rejected")

    async def async_stop(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.status.connected = False

    # --- the stream ---------------------------------------------------------------

    async def _run(self) -> None:
        failures = 0
        while True:
            started = time.monotonic()
            self._frames_this_stream = 0
            try:
                await self.auth.async_get_token()  # valid for at least 5 min at connect
                async with asyncio.timeout(self._stream_max_age):
                    async for raw in self._transport.async_stream(OBSERVE_PATH, self._request):
                        self._handle(raw)
                planned, reason = False, "server closed the stream"
            except TimeoutError:
                planned, reason = True, "planned reconnect before the 60-minute limit"
            except CredentialsRejected as err:
                self._auth_failed(str(err))
                return
            except (AuthExpired, AuthUnavailable, TransportError, codec.StreamError) as err:
                planned, reason = False, f"{type(err).__name__}: {err}"
                self.status.last_error = reason
            self.status.connected = False
            if self._frames_this_stream and not self._ready.is_set():
                self._ready.set()  # a stream that ended has delivered its whole snapshot
            # A healthy stream runs until our planned reconnect or the server's
            # 60-minute limit; anything much shorter is a failure worth backing off.
            if planned or time.monotonic() - started > 600:
                failures, wait = 0, 0
            else:
                failures += 1
                wait = _BACKOFF[min(failures, len(_BACKOFF)) - 1]
            _LOGGER.debug("Observe stream ended (%s); reconnecting in %ss", reason, wait)
            await asyncio.sleep(wait)

    def _handle(self, raw: bytes) -> None:
        self._frames_this_stream += 1
        self.status.connected = True
        self.status.last_error = None
        changes = self.cache.apply_frame(codec.ObserveFrame.FromString(raw))
        self._track_confirmations(changes)
        if not self._ready.is_set():
            # The initial snapshot is complete once the stream goes quiet.
            if self._settle_timer is not None:
                self._settle_timer.cancel()
            self._settle_timer = asyncio.get_running_loop().call_later(SETTLE_SECONDS, self._ready.set)
            return
        if changes:
            for listener in list(self._listeners):
                try:
                    listener(changes)
                except Exception:
                    _LOGGER.exception("protonest change listener failed")

    def _track_confirmations(self, changes: list[TraitChange]) -> None:
        now = time.monotonic()
        for change in changes:
            if change.state_type != CONFIRMED:
                continue
            key = (change.resource_id, change.label)
            self._last_confirmed[change.resource_id] = now
            sents = self._pending.pop(key, None)
            if sents:
                # One of ours, maybe not the last yet; anything else replaced them.
                hit = next((i for i, sent in enumerate(sents) if change.new.message == sent.message), None)
                if hit is None:
                    self._overridden(key)
                elif hit + 1 < len(sents):
                    self._pending[key] = sents[hit + 1:]
                else:
                    self._recent[key] = _Sent(sents[hit].message, now)
                continue
            recent = self._recent.pop(key, None)
            if recent is not None and now - recent.at <= OVERRIDE_WINDOW and change.new.message != recent.message:
                self._overridden(key)

    def _overridden(self, key: tuple[str, str]) -> None:
        _LOGGER.info("%s replaced the change to %s with its own value", key[0], key[1])
        for listener in list(self._override_listeners):
            try:
                listener(*key)
            except Exception:
                _LOGGER.exception("protonest override listener failed")

    def _notify(self) -> None:
        """Wake listeners with no trait changes, e.g. to re-evaluate responsiveness."""
        for listener in list(self._listeners):
            try:
                listener([])
            except Exception:
                _LOGGER.exception("protonest change listener failed")

    def _auth_failed(self, message: str) -> None:
        _LOGGER.warning("Nest rejected the credentials: %s", message)
        self.status = ConnectionStatus(connected=False, last_error=message, auth_failed=True)
        self._auth_failed_event.set()
        for listener in list(self._auth_listeners):
            listener()

    # --- writes ----------------------------------------------------------------------

    async def async_update(
        self,
        resource_id: str,
        label: str,
        mutate: Callable[[pb_message.Message], None],
        check: Callable[[pb_message.Message], None] | None = None,
    ) -> v1_pb2.TraitOperation | None:
        """Read-modify-write one trait. ``check`` may veto the new state (raise).

        Writes to the same trait run one at a time, each building on the last.
        """
        key = (resource_id, label)
        async with self._write_locks.setdefault(key, asyncio.Lock()):
            current = self.cache.latest(resource_id, label)
            now_shown = current.message if current is not None else None
            unechoed = self._unechoed.get(key)
            base = None
            if unechoed is not None:
                ours, last = unechoed
                if now_shown is not None and now_shown == last:
                    unechoed = None  # the stream has caught up
                elif any(now_shown == m for m in ours):
                    base = last  # the stream is still on our own earlier values
                else:
                    unechoed = None  # changed elsewhere: build on that
            if unechoed is None:
                self._unechoed.pop(key, None)
            prepared = prepare_update(self.cache, resource_id, label, mutate, base=base)
            if check is not None:
                check(prepared.new)
            try:
                op = await self._writer.async_send(prepared)
            except CredentialsRejected as err:
                self._auth_failed(str(err))
                raise
            if op is not None:
                ours = unechoed[0] if unechoed is not None else ([now_shown] if now_shown is not None else [])
                self._unechoed[key] = ([*ours, prepared.new], prepared.new)
                self._pending.setdefault(key, []).append(_Sent(prepared.new, time.monotonic()))
                self._recent.pop(key, None)
                # Re-check once the confirmation deadline has passed.
                loop = asyncio.get_running_loop()
                self._timers = [t for t in self._timers if t.when() > loop.time()]
                self._timers.append(loop.call_later(self._unresponsive_after + 0.1, self._notify))
            return op
