"""Fan schedule and manual timer writes.

The strongest test replays a live recording made while the Nest app changed
A thermostat's fan settings (2026-10-04): for every write the app made, applying our
mutation to the state before it must give exactly the state the thermostat
confirmed after it.
"""

from datetime import UTC, datetime

import pytest

from pynestproto import codec
from pynestproto.observe import CONFIRMED, TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.write import (
    FAN_RUN_TIMES,
    FAN_TIMER_DURATIONS,
    prepare_update,
    run_fan,
    set_fan_run_duration,
    set_fan_schedule,
    stop_fan,
)

FAN = hvac_pb2.FanControlSettingsTrait
from .home import THERMOSTAT3
RECORDING = "observe-20261004-001420.bin"
UNTOUCHABLE = ("hvacOverrideSpeed", "scheduleSpeed", "timerSpeed", "timerEquipment",
               "smartVentilationEnabled", "ventilationEnabled")


def thermostat3_like() -> hvac_pb2.FanControlSettingsTrait:
    t = FAN(
        mode=FAN.FAN_MODE_DUTY_CYCLE,
        hvacOverrideSpeed=hvac_pb2.FanControlTrait.FAN_SPEED_SETTING_AUTO,
        scheduleSpeed=hvac_pb2.FanControlTrait.FAN_SPEED_SETTING_STAGE1,
        timerSpeed=hvac_pb2.FanControlTrait.FAN_SPEED_SETTING_STAGE1,
        scheduleDutyCycle=900,
        scheduleStartTime=32400,
        scheduleEndTime=82800,
    )
    t.timerDuration.FromSeconds(1800)
    t.timerEnd.SetInParent()
    return t


def mutated(trait, mutate):
    out = type(trait)()
    out.CopyFrom(trait)
    mutate(out)
    return out


def mutation_for(old, new):
    """The mutation the app's write corresponds to, from what changed."""
    if new.timerEnd.seconds and new.timerEnd != old.timerEnd:
        start = datetime.fromtimestamp(new.timerEnd.seconds - new.timerDuration.seconds, UTC)
        return run_fan(new.timerDuration.seconds, now=start)
    if not new.timerEnd.seconds and old.timerEnd.seconds:
        return stop_fan()
    kwargs = {}
    if new.mode != old.mode:
        kwargs["enabled"] = new.mode == FAN.FAN_MODE_DUTY_CYCLE
    if new.scheduleDutyCycle != old.scheduleDutyCycle:
        kwargs["run_seconds_per_hour"] = new.scheduleDutyCycle
    if new.scheduleStartTime != old.scheduleStartTime:
        kwargs["start_hour"] = new.scheduleStartTime // 3600
    if new.scheduleEndTime != old.scheduleEndTime:
        kwargs["end_hour"] = new.scheduleEndTime // 3600
    assert kwargs, "unrecognized change"
    return set_fan_schedule(**kwargs)


def test_every_app_fan_write_is_reproduced(capture):
    cache = TraitCache()
    checked = 0
    for raw in codec.parse_stream_body(capture(RECORDING)):
        for ch in cache.apply_frame(codec.ObserveFrame.FromString(raw)):
            if (ch.resource_id, ch.label, ch.state_type) != (THERMOSTAT3, "fan_control_settings", CONFIRMED):
                continue
            if ch.is_new:
                continue
            old, new = ch.old.message, ch.new.message
            assert mutated(old, mutation_for(old, new)) == new
            checked += 1
    # The app's 18 writes: run time x3, start x1, end x8 (one per picker step,
    # incl. an overnight wrap and start == end), schedule off/on, a manual
    # 15 min run and its stop. Later recordings may add more.
    assert checked >= 18


def test_schedule_changes_only_requested_fields():
    before = thermostat3_like()
    after = mutated(before, set_fan_schedule(run_seconds_per_hour=1800, start_hour=22, end_hour=6))
    assert (after.scheduleDutyCycle, after.scheduleStartTime, after.scheduleEndTime) == (1800, 79200, 21600)
    for field in ("mode", "timerDuration", "timerEnd", *UNTOUCHABLE):
        assert getattr(after, field) == getattr(before, field)


def test_schedule_off_and_on_only_switch_mode():
    off = mutated(thermostat3_like(), set_fan_schedule(enabled=False))
    assert off.mode == FAN.FAN_MODE_AUTO and off.scheduleDutyCycle == 900
    assert mutated(off, set_fan_schedule(enabled=True)) == thermostat3_like()


@pytest.mark.parametrize(
    "kwargs",
    [{"run_seconds_per_hour": 1200}, {"start_hour": 24}, {"end_hour": -1}, {"start_hour": 9.5}],
)
def test_schedule_rejects_values_the_app_never_sends(kwargs):
    with pytest.raises(ValueError):
        set_fan_schedule(**kwargs)


def test_run_fan_uses_remembered_duration_by_default():
    now = datetime(2026, 10, 4, 6, 20, 31, 500000, tzinfo=UTC)
    after = mutated(thermostat3_like(), run_fan(now=now))
    assert after.timerDuration.seconds == 1800
    assert after.timerEnd.seconds == int(now.replace(microsecond=0).timestamp()) + 1800


def test_run_fan_without_remembered_duration_refuses():
    t = thermostat3_like()
    t.ClearField("timerDuration")
    with pytest.raises(ValueError, match="remembered"):
        mutated(t, run_fan())


def test_stop_fan_matches_servers_idle_form_and_is_noop_when_idle():
    idle = thermostat3_like()
    running = mutated(idle, run_fan(900))
    assert mutated(running, stop_fan()).timerEnd == idle.timerEnd
    assert mutated(idle, stop_fan()) == idle


def test_option_lists_match_app_and_device_limits():
    assert FAN_RUN_TIMES == (900, 1800, 2700, 3600)
    assert FAN_TIMER_DURATIONS[-1] == 604800  # the thermostats' maxTimerDuration
    with pytest.raises(ValueError):
        set_fan_run_duration(1000)
    with pytest.raises(ValueError):
        run_fan(604801)


def test_fan_write_goes_through_prepare_update():
    cache = TraitCache()
    from pynestproto.proto_gen.nestlabs.gateway import v2_pb2

    cache.apply_frame(codec.ObserveFrame(traitStates=[v2_pb2.TraitState(
        traitId=v2_pb2.TraitId(resourceId=THERMOSTAT3, traitLabel="fan_control_settings"),
        stateTypes=[CONFIRMED], patch=v2_pb2.Patch(values=codec.pack(thermostat3_like())), monotonicVersion=1,
    )]))
    prepared = prepare_update(cache, THERMOSTAT3, "fan_control_settings", set_fan_schedule(start_hour=9))
    assert not prepared.changed  # already 9 AM: nothing would be sent
    prepared = prepare_update(cache, THERMOSTAT3, "fan_control_settings", set_fan_schedule(start_hour=8))
    assert codec.unpack(prepared.request.batchUpdateStateRequest[0].state).scheduleStartTime == 28800
