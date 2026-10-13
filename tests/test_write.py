"""Write path tests: read-modify-write against captured state and a fake server."""

from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp import DummyCookieJar, web
from aiohttp.test_utils import TestServer

from pynestproto import codec
from pynestproto.auth import NestToken
from pynestproto.observe import ACCEPTED, CONFIRMED, TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.proto_gen.nestlabs.gateway import v1_pb2, v2_pb2
from pynestproto.transport import BATCH_UPDATE_STATE_PATH, NestTransport
from pynestproto.write import (
    NestWriter,
    WriteError,
    check_target_temperature,
    prepare_update,
    set_hvac_mode,
    set_target_temperature,
)

from .home import THERMOSTAT1, USER
LABEL = "target_temperature_settings"
SP = hvac_pb2.SetPointScheduleSettingsTrait


@pytest.fixture
def fixture_cache(capture):
    cache = TraitCache()
    for frame in codec.parse_observe(capture("nest_proto")):
        cache.apply_frame(frame)
    return cache


def test_read_modify_write_reproduces_web_app_request(fixture_cache, capture):
    """Captured state (Jan 21) + the change the web app made (Jan 25) must
    produce the bytes the web app sent, with one known exception.

    The web app serializes ``originalActorInfo`` with an empty originator and
    an epoch timestamp inside (3a 04 12 00 1a 00); the server stores and
    streams it as an empty message (3a 00), including after the Android app's
    writes. Read-modify-write sends back the server's form, which is the
    point of read-modify-write, so the golden request is normalized to it.
    """
    prepared = prepare_update(
        fixture_cache,
        THERMOSTAT1,
        LABEL,
        set_target_temperature(
            heat=19.91452, user=USER, when=datetime(2026, 1, 25, 22, 31, 39, tzinfo=UTC)
        ),
        request_id="e739720f-e7c3-4e39-8b29-4e6aed61fc83",
    )
    assert prepared.changed

    golden = v1_pb2.BatchUpdateStateRequest.FromString(capture("heat_update_request.bin"))
    state = golden.batchUpdateStateRequest[0].state
    trait = codec.unpack(state)
    assert trait.targetTemperature.originalActorInfo.SerializeToString() == b"\x12\x00\x1a\x00"
    trait.targetTemperature.originalActorInfo.Clear()
    trait.targetTemperature.originalActorInfo.SetInParent()
    state.CopyFrom(codec.pack(trait))

    assert prepared.request.SerializeToString() == golden.SerializeToString()


def test_off_keeps_mode_and_sends_explicit_false(fixture_cache):
    prepared = prepare_update(fixture_cache, THERMOSTAT1, LABEL, set_hvac_mode("off", user=USER))
    new = prepared.new
    assert new.HasField("enabled") and new.enabled.value is False  # explicit off, not "unset"
    assert new.targetTemperature.setpointType == prepared.old.targetTemperature.setpointType
    assert new.targetTemperature.coolingTarget == prepared.old.targetTemperature.coolingTarget
    # The whole trait goes out, including fields we didn't touch.
    sent = codec.unpack(prepared.request.batchUpdateStateRequest[0].state)
    assert sent == new


def test_mode_change_sets_type_and_enables(fixture_cache):
    new = prepare_update(fixture_cache, THERMOSTAT1, LABEL, set_hvac_mode("cool")).new
    assert new.enabled.value is True
    assert new.targetTemperature.setpointType == SP.SET_POINT_TYPE_COOL


def test_actor_is_stamped(fixture_cache):
    when = datetime(2026, 10, 3, 12, 0, 0, 999999, tzinfo=UTC)
    actor = prepare_update(
        fixture_cache, THERMOSTAT1, LABEL, set_target_temperature(cool=24.0, user=USER, when=when)
    ).new.targetTemperature.currentActorInfo
    assert actor.method == hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_WEB
    assert actor.originator.resourceId == USER
    assert actor.timeOfAction.ToDatetime(UTC) == when.replace(microsecond=0)


def test_refuses_to_write_without_cached_state():
    with pytest.raises(WriteError, match="refusing"):
        prepare_update(TraitCache(), THERMOSTAT1, LABEL, set_hvac_mode("cool"))


@pytest.mark.parametrize("bad", [75.0, -5.0, float("nan")])
def test_rejects_implausible_celsius(bad):
    with pytest.raises(ValueError, match="Celsius"):
        set_target_temperature(cool=bad)


def test_rejects_unknown_mode():
    with pytest.raises(ValueError):
        set_hvac_mode("auto")


def test_builds_on_pending_accepted_value():
    """A newer ACCEPTED value must be the base, or a write would undo it."""
    cache = TraitCache()

    def trait(cool):
        t = hvac_pb2.TargetTemperatureSettingsTrait()
        t.targetTemperature.coolingTarget.value = cool
        return t

    def ts(msg, version, kind):
        return v2_pb2.TraitState(
            traitId=v2_pb2.TraitId(resourceId=THERMOSTAT1, traitLabel=LABEL),
            stateTypes=[kind],
            patch=v2_pb2.Patch(values=codec.pack(msg)),
            monotonicVersion=version,
        )

    cache.apply_frame(codec.ObserveFrame(traitStates=[ts(trait(24.0), 1, CONFIRMED), ts(trait(23.0), 2, ACCEPTED)]))
    old = prepare_update(cache, THERMOSTAT1, LABEL, set_hvac_mode("cool")).old
    assert old.targetTemperature.coolingTarget.value == 23.0


# --- NestWriter against a fake gateway ---------------------------------------


class StubAuth:
    def __init__(self):
        self.invalidated = 0

    async def async_get_token(self, min_validity=300):
        return NestToken(jwt="g.0.test", user_id="1", expires_at=datetime(2100, 1, 1, tzinfo=UTC))

    def invalidate(self):
        self.invalidated += 1


@pytest.fixture
async def gateway(capture):
    state = {"requests": [], "statuses": [], "body": capture("heat_update_response.bin")}

    async def handler(request: web.Request) -> web.Response:
        state["requests"].append((dict(request.headers), await request.read()))
        status = state["statuses"].pop(0) if state["statuses"] else 200
        return web.Response(body=state["body"] if status == 200 else b"", status=status)

    app = web.Application()
    app.router.add_post(BATCH_UPDATE_STATE_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    state["url"] = str(server.make_url("")).rstrip("/")
    yield state
    await server.close()


async def make_writer(gateway, cache):
    session = aiohttp.ClientSession(cookie_jar=DummyCookieJar())
    auth = StubAuth()
    return session, auth, NestWriter(NestTransport(session, auth, base_url=gateway["url"]), cache)


async def test_writer_sends_request_and_parses_result(gateway, fixture_cache):
    session, _, writer = await make_writer(gateway, fixture_cache)
    async with session:
        prepared = prepare_update(fixture_cache, THERMOSTAT1, LABEL, set_target_temperature(heat=19.91452, user=USER))
        op = await writer.async_send(prepared)
    assert op.progress == v1_pb2.TraitOperation.COMPLETE
    ((headers, body),) = gateway["requests"]
    assert body == prepared.request.SerializeToString()
    assert headers["Authorization"] == "Basic g.0.test"
    assert headers["Content-Type"] == "application/x-protobuf"


async def test_setting_current_values_sends_nothing(gateway, fixture_cache):
    session, _, writer = await make_writer(gateway, fixture_cache)
    tt = fixture_cache.latest(THERMOSTAT1, LABEL).message.targetTemperature
    async with session:
        assert await writer.async_update(
            THERMOSTAT1, LABEL, set_target_temperature(cool=tt.coolingTarget.value, user=USER)
        ) is None
        assert await writer.async_update(THERMOSTAT1, LABEL, set_hvac_mode("heat", user=USER)) is None
    assert gateway["requests"] == []


async def test_rejected_token_is_retried_once(gateway, fixture_cache):
    gateway["statuses"] = [401]
    session, auth, writer = await make_writer(gateway, fixture_cache)
    async with session:
        await writer.async_update(THERMOSTAT1, LABEL, set_hvac_mode("cool"))
    assert auth.invalidated == 1
    assert len(gateway["requests"]) == 2


async def test_server_side_rejection_raises(gateway, fixture_cache):
    resp = codec.UpdateStateFrame()
    op = resp.traitOperations.add()
    op.status.code = 3
    op.status.message = "INVALID_ARGUMENT"
    from pynestproto.proto_gen.google.rpc import stream_body_pb2

    gateway["body"] = stream_body_pb2.StreamBody(message=[resp.SerializeToString()]).SerializeToString()
    session, _, writer = await make_writer(gateway, fixture_cache)
    async with session:
        with pytest.raises(WriteError, match="INVALID_ARGUMENT"):
            await writer.async_update(THERMOSTAT1, LABEL, set_hvac_mode("cool"))


def _trait(kind, heat=0.0, cool=0.0, enabled=True):
    t = hvac_pb2.TargetTemperatureSettingsTrait()
    t.enabled.value = enabled
    t.targetTemperature.setpointType = kind
    t.targetTemperature.heatingTarget.value = heat
    t.targetTemperature.coolingTarget.value = cool
    return t


@pytest.mark.parametrize(
    ("trait", "complaint"),
    [
        (_trait(SP.SET_POINT_TYPE_HEAT, cool=24.0), "heating setpoint"),  # cool-only thermostat -> heat
        (_trait(SP.SET_POINT_TYPE_RANGE, cool=24.0), "heating setpoint"),
        (_trait(SP.SET_POINT_TYPE_COOL, heat=20.0), "cooling setpoint"),
        (_trait(SP.SET_POINT_TYPE_RANGE, heat=25.0, cool=22.0), "below the cooling"),
    ],
)
def test_check_rejects_missing_or_inverted_setpoints(trait, complaint):
    with pytest.raises(ValueError, match=complaint):
        check_target_temperature(trait)


@pytest.mark.parametrize(
    "trait",
    [
        _trait(SP.SET_POINT_TYPE_COOL, cool=24.0),
        _trait(SP.SET_POINT_TYPE_HEAT, heat=20.0),
        _trait(SP.SET_POINT_TYPE_RANGE, heat=20.0, cool=24.0),
        _trait(SP.SET_POINT_TYPE_HEAT, enabled=False),  # off: setpoints unused
    ],
)
def test_check_accepts_sensible_states(trait):
    check_target_temperature(trait)
