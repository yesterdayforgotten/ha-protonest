"""Filter reminder: replays the Nest app's "last changed" picks on one thermostat
(2026-10-04) and checks the threshold write, which the app doesn't expose."""

from datetime import UTC, datetime

import pytest

from pynestproto import codec
from pynestproto.observe import CONFIRMED, TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.write import FILTER_OVER_6_MONTHS, set_filter_changed, set_filter_threshold, set_flag

from .home import THERMOSTAT1


def mutated(trait, mutate):
    out = type(trait)()
    out.CopyFrom(trait)
    mutate(out)
    return out


def test_app_filter_writes_reproduced(capture):
    cache = TraitCache()
    seen = []
    for raw in codec.parse_stream_body(capture("observe-20261004-093231.bin")):
        for ch in cache.apply_frame(codec.ObserveFrame.FromString(raw)):
            if (ch.resource_id, ch.label, ch.state_type) != (THERMOSTAT1, "filter_reminder_settings", CONFIRMED) or ch.is_new:
                continue
            old, new = ch.old.message, ch.new.message
            if old.filterReminderEnabled != new.filterReminderEnabled:
                mutate = set_flag("filter_reminder", new.filterReminderEnabled)
                seen.append("toggle")
            elif old.filterChangedDate != new.filterChangedDate:
                secs = new.filterChangedDate.seconds
                mutate = set_filter_changed(None if secs == FILTER_OVER_6_MONTHS else datetime.fromtimestamp(secs, UTC))
                seen.append("over6" if secs == FILTER_OVER_6_MONTHS else new.filterChangedDate.ToDatetime().strftime("%m-%d"))
            elif old.filterReplacementThreshold != new.filterReplacementThreshold:
                mutate = set_filter_threshold(new.filterReplacementThreshold.seconds / 3600)
                seen.append(f"{new.filterReplacementThreshold.seconds // 3600}h")
            else:
                continue
            assert mutated(old, mutate) == new
    # app: off, on, Oct/Sep/Aug (stored as the 15th), "more than 6 months";
    # then our threshold test: 400 h and back to 500 h
    assert seen[:6] == ["toggle", "toggle", "10-15", "09-15", "08-15", "over6"]
    assert seen[6:8] == ["400h", "500h"]


def test_threshold_bounds():
    t = mutated(hvac_pb2.FilterReminderSettingsTrait(), set_filter_threshold(1800))
    assert t.filterReplacementThreshold.seconds == 1800 * 3600
    with pytest.raises(ValueError):
        set_filter_threshold(20)
