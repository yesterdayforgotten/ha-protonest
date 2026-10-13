"""NestClient against a fake gateway that streams the captured snapshot."""

import asyncio
from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp import DummyCookieJar, web
from aiohttp.test_utils import TestServer

from pynestproto import codec
from pynestproto.auth import CredentialsRejected, GoogleCredentials, NestToken
from pynestproto.client import NestClient
from pynestproto.observe import CONFIRMED
from pynestproto.proto_gen.google.rpc import stream_body_pb2
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.proto_gen.nestlabs.gateway import v1_pb2, v2_pb2
from pynestproto.transport import BATCH_UPDATE_STATE_PATH, OBSERVE_PATH, NestTransport
from pynestproto.write import set_target_temperature

from .home import THERMOSTAT2, THERMOSTAT1


class StubAuth:
    def __init__(self, reject=False):
        self.reject = reject

    async def async_get_token(self, min_validity=300):
        if self.reject:
            raise CredentialsRejected("No active session found.")
        return NestToken(jwt="g.0.test", user_id="1", expires_at=datetime(2100, 1, 1, tzinfo=UTC))

    def invalidate(self):
        pass


def changed_frame(cool: float, version: int) -> bytes:
    trait = hvac_pb2.TargetTemperatureSettingsTrait()
    trait.enabled.value = True
    trait.targetTemperature.coolingTarget.value = cool
    ts = v2_pb2.TraitState(
        traitId=v2_pb2.TraitId(resourceId=THERMOSTAT2, traitLabel="target_temperature_settings"),
        stateTypes=[CONFIRMED], patch=v2_pb2.Patch(values=codec.pack(trait)), monotonicVersion=version,
    )
    return codec.ObserveFrame(traitStates=[ts]).SerializeToString()


class FakeGateway:
    def __init__(self, snapshot: list[bytes], write_response: bytes):
        self.snapshot = snapshot
        self.write_response = write_response
        self.connections = 0
        self.close_after_snapshot = False
        self.pushes: asyncio.Queue[bytes] = asyncio.Queue()
        self.writes: list[bytes] = []

    async def observe(self, request: web.Request) -> web.StreamResponse:
        self.connections += 1
        resp = web.StreamResponse()
        await resp.prepare(request)
        for msg in self.snapshot:
            await resp.write(stream_body_pb2.StreamBody(message=[msg]).SerializeToString())
        if not self.close_after_snapshot:
            while True:
                msg = await self.pushes.get()
                await resp.write(stream_body_pb2.StreamBody(message=[msg]).SerializeToString())
        return resp

    async def write(self, request: web.Request) -> web.Response:
        self.writes.append(await request.read())
        return web.Response(body=self.write_response)


@pytest.fixture
async def gateway(capture):
    gw = FakeGateway(codec.parse_stream_body(capture("nest_proto")), capture("heat_update_response.bin"))
    app = web.Application()
    app.router.add_post(OBSERVE_PATH, gw.observe)
    app.router.add_post(BATCH_UPDATE_STATE_PATH, gw.write)
    server = TestServer(app)
    await server.start_server()
    gw.url = str(server.make_url("")).rstrip("/")
    yield gw
    await server.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession(cookie_jar=DummyCookieJar()) as s:
        yield s


def make_client(session, gateway, auth=None, **kwargs):
    auth = auth or StubAuth()
    creds = GoogleCredentials(issue_token="https://example.invalid", cookies="SID=x")
    return NestClient(session, creds, auth=auth, transport=NestTransport(session, auth, base_url=gateway.url), **kwargs)


async def test_ready_after_snapshot(session, gateway):
    client = make_client(session, gateway)
    client.start()
    await client.async_wait_ready(timeout=10)
    assert client.ready and client.status.connected
    assert len(client.cache.resource_types) == 10
    await client.async_stop()


async def test_changes_after_snapshot_reach_listeners(session, gateway):
    client = make_client(session, gateway)
    got = []
    client.add_listener(got.extend)
    client.start()
    await client.async_wait_ready(timeout=10)
    assert got == []  # the snapshot itself is not reported as changes
    await gateway.pushes.put(changed_frame(24.0, 9_999_999_999_999_999))
    await asyncio.sleep(0.3)
    assert [(c.resource_id, c.label) for c in got] == [(THERMOSTAT2, "target_temperature_settings")]
    await client.async_stop()


async def test_reconnects_when_server_closes_without_spurious_changes(session, gateway):
    gateway.close_after_snapshot = True
    client = make_client(session, gateway)
    got = []
    client.add_listener(got.extend)
    client.start()
    await client.async_wait_ready(timeout=10)
    await asyncio.sleep(3.0)  # an early close is retried after a 2 s backoff, not instantly
    assert 2 <= gateway.connections <= 3
    assert got == []  # the resent snapshot matched the cache
    await client.async_stop()


async def test_planned_reconnect_before_max_age(session, gateway):
    client = make_client(session, gateway, stream_max_age=0.5)
    client.start()
    await client.async_wait_ready(timeout=10)
    await asyncio.sleep(1.5)
    assert gateway.connections >= 2
    await client.async_stop()


async def test_rejected_credentials_stop_and_report(session, gateway):
    client = make_client(session, gateway, auth=StubAuth(reject=True))
    failures = []
    client.add_auth_failure_listener(lambda: failures.append(1))
    client.start()
    with pytest.raises(CredentialsRejected):
        await client.async_wait_ready(timeout=5)
    assert client.status.auth_failed and failures == [1]
    assert gateway.connections == 0
    await client.async_stop()


async def test_write_through_client(session, gateway):
    client = make_client(session, gateway)
    client.start()
    await client.async_wait_ready(timeout=10)
    op = await client.async_update(THERMOSTAT1, "target_temperature_settings", set_target_temperature(cool=24.0))
    assert op.progress == v1_pb2.TraitOperation.COMPLETE
    sent = v1_pb2.BatchUpdateStateRequest.FromString(gateway.writes[0]).batchUpdateStateRequest[0]
    assert codec.unpack(sent.state).targetTemperature.coolingTarget.value == 24.0
    await client.async_stop()


async def test_check_can_veto_a_write(session, gateway):
    client = make_client(session, gateway)
    client.start()
    await client.async_wait_ready(timeout=10)

    def veto(_):
        raise ValueError("no")

    with pytest.raises(ValueError):
        await client.async_update(THERMOSTAT1, "target_temperature_settings", set_target_temperature(cool=24.0), check=veto)
    assert gateway.writes == []
    await client.async_stop()


# --- responsiveness and overrides ------------------------------------------------

from pynestproto.proto_gen.weave.trait import heartbeat_pb2  # noqa: E402

TARGET = "target_temperature_settings"


def confirm(rid, label, msg, version):
    ts = v2_pb2.TraitState(
        traitId=v2_pb2.TraitId(resourceId=rid, traitLabel=label), stateTypes=[CONFIRMED],
        patch=v2_pb2.Patch(values=codec.pack(msg)), monotonicVersion=version,
    )
    return codec.ObserveFrame(traitStates=[ts]).SerializeToString()


async def ready_client(session, gateway, **kw):
    client = make_client(session, gateway, unresponsive_after=0.3, **kw)
    client.start()
    await client.async_wait_ready(timeout=10)
    return client


async def test_unconfirmed_write_makes_device_unresponsive_until_it_confirms(session, gateway):
    client = await ready_client(session, gateway)
    woken = []
    client.add_listener(woken.append)
    assert client.responsive(THERMOSTAT1)
    await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(cool=24.0))
    assert client.responsive(THERMOSTAT1)  # not yet: the deadline hasn't passed
    await asyncio.sleep(0.5)
    assert not client.responsive(THERMOSTAT1)
    assert [] in woken  # listeners were woken to re-evaluate
    assert client.responsive(THERMOSTAT2)  # other thermostats unaffected
    sent = client._pending[(THERMOSTAT1, TARGET)].message
    await gateway.pushes.put(confirm(THERMOSTAT1, TARGET, sent, 9_999_999_999_999_990))
    await asyncio.sleep(0.2)
    assert client.responsive(THERMOSTAT1)
    await client.async_stop()


async def test_confirmed_as_sent_is_not_an_override(session, gateway):
    client = await ready_client(session, gateway)
    overrides = []
    client.add_override_listener(lambda rid, label: overrides.append((rid, label)))
    await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(cool=24.0))
    sent = client._pending[(THERMOSTAT1, TARGET)].message
    await gateway.pushes.put(confirm(THERMOSTAT1, TARGET, sent, 9_999_999_999_999_990))
    await asyncio.sleep(0.2)
    assert overrides == []
    await client.async_stop()


async def test_local_value_replacing_ours_is_reported(session, gateway):
    """What a thermostat did on reconnect (2026-10-04): confirm our setpoint, then
    immediately its own local one."""
    client = await ready_client(session, gateway)
    overrides = []
    client.add_override_listener(lambda rid, label: overrides.append((rid, label)))
    await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(cool=24.0))
    sent = client._pending[(THERMOSTAT1, TARGET)].message
    local = type(sent)()
    local.CopyFrom(sent)
    local.targetTemperature.coolingTarget.value = 24.575394
    local.targetTemperature.currentActorInfo.method = hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_LOCAL
    await gateway.pushes.put(confirm(THERMOSTAT1, TARGET, sent, 9_999_999_999_999_990))
    await gateway.pushes.put(confirm(THERMOSTAT1, TARGET, local, 9_999_999_999_999_991))
    await asyncio.sleep(0.2)
    assert overrides == [(THERMOSTAT1, TARGET)]
    await client.async_stop()


async def test_nest_reporting_offline_means_unresponsive(session, gateway):
    client = await ready_client(session, gateway)
    live = heartbeat_pb2.LivenessTrait(status=heartbeat_pb2.LivenessTrait.LIVENESS_DEVICE_STATUS_UNREACHABLE)
    await gateway.pushes.put(confirm(THERMOSTAT2, "liveness", live, 9_999_999_999_999_990))
    await asyncio.sleep(0.2)
    assert not client.responsive(THERMOSTAT2)
    await client.async_stop()


# --- writes to the same trait -------------------------------------------------------


def sent_targets(gateway):
    return [codec.unpack(v1_pb2.BatchUpdateStateRequest.FromString(w).batchUpdateStateRequest[0].state).targetTemperature
            for w in gateway.writes]


async def test_quick_writes_to_one_trait_build_on_each_other(session, gateway):
    """Each write sends the whole trait; a second one sent before the stream
    echoes the first must not send the first's old value back."""
    client = await ready_client(session, gateway)
    await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(cool=24.0))
    await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(heat=18.0))
    last = sent_targets(gateway)[-1]
    assert (last.coolingTarget.value, last.heatingTarget.value) == (24.0, 18.0)
    await client.async_stop()


async def test_concurrent_writes_to_one_trait_both_apply(session, gateway):
    client = await ready_client(session, gateway)
    await asyncio.gather(
        client.async_update(THERMOSTAT1, TARGET, set_target_temperature(cool=24.0)),
        client.async_update(THERMOSTAT1, TARGET, set_target_temperature(heat=18.0)),
    )
    last = sent_targets(gateway)[-1]
    assert (last.coolingTarget.value, last.heatingTarget.value) == (24.0, 18.0)
    await client.async_stop()


async def test_writes_build_on_the_stream_once_it_moves_on(session, gateway):
    """A newer value from the stream (here a change made on the thermostat)
    replaces the remembered sent value as the base."""
    client = await ready_client(session, gateway)
    await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(cool=24.0))
    local = type(client._pending[(THERMOSTAT1, TARGET)].message)()
    local.CopyFrom(client._pending[(THERMOSTAT1, TARGET)].message)
    local.targetTemperature.coolingTarget.value = 25.0
    await gateway.pushes.put(confirm(THERMOSTAT1, TARGET, local, 9_999_999_999_999_990))
    await asyncio.sleep(0.2)
    await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(heat=18.0))
    assert sent_targets(gateway)[-1].coolingTarget.value == 25.0
    await client.async_stop()


async def test_fired_confirmation_timers_are_dropped(session, gateway):
    client = await ready_client(session, gateway)
    for cool in (24.0, 24.5):
        await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(cool=cool))
        await asyncio.sleep(0.5)  # past unresponsive_after: the timer has fired
    await client.async_update(THERMOSTAT1, TARGET, set_target_temperature(cool=25.0))
    assert len(client._timers) == 1
    await client.async_stop()
