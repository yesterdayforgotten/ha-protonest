"""TraitCache and Observe request tests."""

import pytest

from pynestproto import codec
from pynestproto.observe import (
    ACCEPTED,
    CONFIRMED,
    OBSERVED_TRAITS,
    TraitCache,
    build_observe_request,
)
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.proto_gen.nestlabs.gateway import v2_pb2
from pynestproto.proto_gen.weave.trait import description_pb2

from .home import (
    ROOM1_NAME, ROOM2_NAME, ROOM3_NAME, ROOM4_NAME, ROOM5_NAME, THERMOSTAT1_NAME, THERMOSTAT2_NAME, THERMOSTAT3_NAME,
)

DEVICE = "DEVICE_TEST"


def frame(*states, metas=()):
    return codec.ObserveFrame(resourceMetas=list(metas), traitStates=list(states))


def state(msg, version, *state_types, label="target_temperature_settings", rid=DEVICE):
    return v2_pb2.TraitState(
        traitId=v2_pb2.TraitId(resourceId=rid, traitLabel=label),
        stateTypes=list(state_types),
        patch=v2_pb2.Patch(values=codec.pack(msg)),
        monotonicVersion=version,
    )


def heat(target: float) -> hvac_pb2.TargetTemperatureSettingsTrait:
    msg = hvac_pb2.TargetTemperatureSettingsTrait()
    msg.targetTemperature.heatingTarget.value = target
    return msg


def test_observe_request_matches_web_app_shape():
    req = build_observe_request()
    # Same as the captured web app request: state types [ACCEPTED, CONFIRMED],
    # one TraitTypeObserveParams per trait, no user id.
    assert list(req.stateTypes) == [ACCEPTED, CONFIRMED]
    assert len(req.traitTypeParams) == len(OBSERVED_TRAITS)
    assert req.traitTypeParams[0].traitType == "nest.trait.hvac.TargetTemperatureSettingsTrait"
    assert req.SerializeToString().startswith(b"\x0a\x02\x02\x01")  # packed [2, 1], as captured


def test_accepted_and_confirmed_tracked_separately():
    cache = TraitCache()
    changes = cache.apply_frame(frame(state(heat(20), 5, ACCEPTED)))
    assert [c.state_type for c in changes] == [ACCEPTED]
    assert cache.get(DEVICE, "target_temperature_settings") is None  # no CONFIRMED yet
    assert cache.get(DEVICE, "target_temperature_settings", ACCEPTED) == heat(20)

    cache.apply_frame(frame(state(heat(20), 6, CONFIRMED)))
    assert cache.get(DEVICE, "target_temperature_settings") == heat(20)


def test_stale_and_duplicate_versions_are_dropped():
    cache = TraitCache()
    cache.apply_frame(frame(state(heat(21), 10, CONFIRMED)))
    assert cache.apply_frame(frame(state(heat(19), 9, CONFIRMED))) == []  # older
    assert cache.apply_frame(frame(state(heat(19), 10, CONFIRMED))) == []  # same version
    assert cache.get(DEVICE, "target_temperature_settings").targetTemperature.heatingTarget.value == 21


def test_newer_version_with_same_value_is_not_a_change():
    cache = TraitCache()
    cache.apply_frame(frame(state(heat(21), 10, CONFIRMED)))
    assert cache.apply_frame(frame(state(heat(21), 11, CONFIRMED))) == []


def test_change_reports_old_and_new():
    cache = TraitCache()
    (first,) = cache.apply_frame(frame(state(heat(20), 1, CONFIRMED)))
    assert first.is_new
    (change,) = cache.apply_frame(frame(state(heat(22), 2, CONFIRMED)))
    assert not change.is_new
    assert change.old.message == heat(20)
    assert change.new.message == heat(22)


def test_removed_resource_is_dropped():
    cache = TraitCache()
    meta = v2_pb2.ResourceMeta(resourceId=DEVICE, type="nest.resource.NestKryptoniteResource")
    cache.apply_frame(frame(state(heat(20), 1, CONFIRMED), metas=[meta]))
    removed = v2_pb2.ResourceMeta(resourceId=DEVICE, status=v2_pb2.ResourceStatus.REMOVED)
    cache.apply_frame(frame(metas=[removed]))
    assert DEVICE not in cache.resource_types
    assert cache.labels(DEVICE) == set()


def test_custom_label_wins_over_room_name():
    cache = TraitCache()
    cache.apply_frame(
        frame(state(description_pb2.LabelSettingsTrait(label="Hallway"), 1, CONFIRMED, label="label"))
    )
    assert cache.display_name(DEVICE) == "Hallway"


def test_fixture_names_resolve(capture):
    cache = TraitCache()
    for f in codec.parse_observe(capture("nest_proto")):
        cache.apply_frame(f)
    names = {cache.display_name(rid) for rid, t in cache.resource_types.items() if "DEVICE" in rid}
    assert names == {
        THERMOSTAT1_NAME, THERMOSTAT3_NAME, THERMOSTAT2_NAME,
        ROOM5_NAME, ROOM3_NAME, ROOM2_NAME, ROOM4_NAME, ROOM1_NAME,
    }
