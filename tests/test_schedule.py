"""Weekly schedule editing, replayed against the Nest app's edits to Main
Floor's cool schedule (2026-10-04): add an entry, change its temperature
five times, move it, delete it."""

from datetime import UTC

import pytest

from pynestproto import codec
from pynestproto.observe import CONFIRMED, TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.write import (
    DAYS,
    add_schedule_entry,
    remove_schedule_entry,
    update_schedule_entry,
)

SP = hvac_pb2.SetPointScheduleSettingsTrait
from .home import THERMOSTAT1
DAY_NAME = {v: k for k, v in DAYS.items()}


def mutated(trait, mutate):
    out = type(trait)()
    out.CopyFrom(trait)
    mutate(out)
    return out


def content(sp):
    """An entry without its actors: what the user sees."""
    return (sp.dayOfWeek, sp.secondsInDay, sp.setpointType, sp.heatingTarget.value, sp.coolingTarget.value)


def app_edit_to_mutation(old, new):
    before = {content(sp): sp for sp in old.setpoints.values()}
    after = {content(sp): sp for sp in new.setpoints.values()}
    gone = [before[k] for k in before.keys() - after.keys()]
    came = [after[k] for k in after.keys() - before.keys()]
    if not gone and len(came) == 1:  # add
        sp = came[0]
        a = sp.currentActorInfo
        return "add", add_schedule_entry(
            "cool", DAY_NAME[sp.dayOfWeek], sp.secondsInDay, cool=sp.coolingTarget.value,
            user=a.originator.resourceId, when=a.timeOfAction.ToDatetime(UTC), method=a.method)
    if len(gone) == 1 and not came:  # remove
        sp = gone[0]
        return "remove", remove_schedule_entry("cool", DAY_NAME[sp.dayOfWeek], sp.secondsInDay)
    if len(gone) == 1 and len(came) == 1:  # update
        o, n = gone[0], came[0]
        a = n.currentActorInfo
        return "update", update_schedule_entry(
            "cool", DAY_NAME[o.dayOfWeek], o.secondsInDay,
            new_day=DAY_NAME[n.dayOfWeek], new_seconds=n.secondsInDay, cool=n.coolingTarget.value,
            user=a.originator.resourceId, when=a.timeOfAction.ToDatetime(UTC), method=a.method)
    return None, None  # e.g. the auto-schedule re-key: not an edit


def test_every_app_schedule_edit_is_reproduced(capture):
    cache = TraitCache()
    ops = []
    for raw in codec.parse_stream_body(capture("observe-20261004-080746.bin")):
        for ch in cache.apply_frame(codec.ObserveFrame.FromString(raw)):
            if (ch.resource_id, ch.label, ch.state_type) != (THERMOSTAT1, "cool_schedule_settings", CONFIRMED):
                continue
            if ch.is_new:
                continue
            op, mutate = app_edit_to_mutation(ch.old.message, ch.new.message)
            if op is None:
                continue
            assert mutated(ch.old.message, mutate) == ch.new.message, op
            ops.append(op)
    # The app's 8 edits, then (in this recording) our own live add/remove.
    assert ops[:8] == ["add"] + ["update"] * 6 + ["remove"]


def cool_schedule(*entries):
    t = SP(name="Current Schedule", type=SP.SET_POINT_SCHEDULE_TYPE_COOL)
    for i, (day, seconds, c) in enumerate(entries):
        sp = t.setpoints[200 + i]
        sp.setpointType, sp.dayOfWeek, sp.secondsInDay = SP.SET_POINT_TYPE_COOL, DAYS[day], seconds
        sp.heatingTarget.value, sp.coolingTarget.value = 9.0, c
    return t


def test_renumbered_monday_first_and_blank_actors_filled():
    t = mutated(cool_schedule(("sun", 3600, 25.0), ("mon", 7200, 24.0)),
                add_schedule_entry("cool", "mon", 3600, cool=26.0, user="USER_X"))
    days = [(t.setpoints[k].dayOfWeek, t.setpoints[k].secondsInDay) for k in sorted(t.setpoints)]
    assert sorted(t.setpoints) == [0, 1, 2]
    assert days == [(DAYS["mon"], 3600), (DAYS["mon"], 7200), (DAYS["sun"], 3600)]
    assert t.setpoints[1].originalActorInfo.method == hvac_pb2.HvacActor.HVAC_ACTOR_METHOD_NOBODY


@pytest.mark.parametrize(
    ("make", "complaint"),
    [
        (lambda: add_schedule_entry("cool", "mon", 3600 + 60, cool=24.0), "quarter hour"),
        (lambda: add_schedule_entry("cool", "funday", 3600, cool=24.0), "day"),
        (lambda: add_schedule_entry("cool", "mon", 3600, cool=40.0), "50-90"),
        (lambda: add_schedule_entry("cool", "mon", 3600), "needs a cool"),
        (lambda: add_schedule_entry("range", "mon", 3600, heat=25.0, cool=22.0), "below"),
    ],
)
def test_schedule_validation(make, complaint):
    with pytest.raises(ValueError, match=complaint):
        make()


def test_duplicate_and_missing_entries_refused():
    t = cool_schedule(("mon", 3600, 24.0))
    with pytest.raises(ValueError, match="already"):
        mutated(t, add_schedule_entry("cool", "mon", 3600, cool=25.0))
    with pytest.raises(ValueError, match="no schedule entry"):
        mutated(t, remove_schedule_entry("cool", "tue", 3600))
