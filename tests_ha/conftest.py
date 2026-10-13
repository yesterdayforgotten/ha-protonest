"""Home Assistant layer tests (run in the protonest-test container; see dev/Dockerfile.test).

The integration runs in a real test Home Assistant; only NestClient is replaced,
by FakeClient: it serves a real recorded snapshot and applies writes the way
the thermostat confirms them, pushing the change back to listeners.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.protonest.pynestproto import codec
from custom_components.protonest.pynestproto.auth import CredentialsRejected
from custom_components.protonest.pynestproto.client import ConnectionStatus
from custom_components.protonest.pynestproto.observe import CONFIRMED, TraitCache
from custom_components.protonest.pynestproto.proto_gen.nestlabs.gateway import v2_pb2
from custom_components.protonest.pynestproto.write import prepare_update

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "captures" / "observe-20261004-100649.bin"  # gitignored: real home state

codec.load_trait_types()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeClient:
    reject = False
    instances: list["FakeClient"] = []

    def __init__(self, session, credentials, on_credentials_changed=None, **kwargs) -> None:
        self.credentials = credentials
        self.on_credentials_changed = on_credentials_changed
        self.cache = TraitCache()
        for raw in codec.parse_stream_body(SNAPSHOT.read_bytes()):
            self.cache.apply_frame(codec.ObserveFrame.FromString(raw))
        self.status = ConnectionStatus(connected=True)
        self.ready = True
        self.writes: list[tuple[str, str, object]] = []
        self._listeners = []
        self._auth_listeners = []
        self._version = 9_000_000_000_000_000
        self._override_listeners = []
        self.unresponsive: set[str] = set()
        FakeClient.instances.append(self)

    def responsive(self, resource_id: str) -> bool:
        return resource_id not in self.unresponsive

    def add_override_listener(self, listener):
        self._override_listeners.append(listener)
        return lambda: self._override_listeners.remove(listener)

    def set_responsive(self, resource_id: str, ok: bool) -> None:
        (self.unresponsive.discard if ok else self.unresponsive.add)(resource_id)
        for listener in list(self._listeners):
            listener([])

    def override(self, resource_id: str, label: str) -> None:
        for listener in list(self._override_listeners):
            listener(resource_id, label)

    def start(self) -> None:
        pass

    async def async_wait_ready(self, timeout: float = 30) -> None:
        if FakeClient.reject:
            raise CredentialsRejected("No active session found.")

    async def async_stop(self) -> None:
        pass

    def add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def add_auth_failure_listener(self, listener):
        self._auth_listeners.append(listener)
        return lambda: self._auth_listeners.remove(listener)

    def fail_auth(self) -> None:
        self.status = ConnectionStatus(connected=False, auth_failed=True)
        for listener in list(self._auth_listeners):
            listener()

    async def async_update(self, resource_id, label, mutate, check=None):
        prepared = prepare_update(self.cache, resource_id, label, mutate)
        if check is not None:
            check(prepared.new)
        if not prepared.changed:
            return None
        self.writes.append((resource_id, label, prepared.new))
        self._version += 1
        state = v2_pb2.TraitState(
            traitId=v2_pb2.TraitId(resourceId=resource_id, traitLabel=label),
            stateTypes=[CONFIRMED],
            patch=v2_pb2.Patch(values=codec.pack(prepared.new)),
            monotonicVersion=self._version,
        )
        changes = self.cache.apply_frame(codec.ObserveFrame(traitStates=[state]))
        for listener in list(self._listeners):
            listener(changes)
        return None

    def last_write(self, label: str):
        return next(new for _, lbl, new in reversed(self.writes) if lbl == label)


@pytest.fixture
def fake_client(monkeypatch):
    if not SNAPSHOT.exists():
        pytest.skip(f"snapshot not available: {SNAPSHOT}")
    FakeClient.reject = False
    FakeClient.instances = []
    monkeypatch.setattr("custom_components.protonest.NestClient", FakeClient)
    return FakeClient
