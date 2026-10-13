"""Thermostat settings: auto-schedule, home/away assist, Early-On, cool to dry,
sunblock, airwave, eco and safety temperatures, display units, sensor pairing.

Replays a recording (every trait subscribed) made while the Nest app toggled
each setting on one thermostat (2026-10-04): every app change must be reproduced
exactly by our mutation, applied to the state before it.
"""

import pytest

from pynestproto import codec
from pynestproto.observe import CONFIRMED, TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2
from pynestproto.write import (
    SETTING_FLAGS,
    THRESHOLDS,
    pair_sensor,
    set_early_on,
    set_flag,
    set_temperature_scale,
    set_threshold,
    unpair_sensor,
)

RECORDING = "observe-20261004-080746.bin"
from .home import THERMOSTAT1
from .home import ROOM3
SCALE = hvac_pb2.DisplaySettingsTrait


def mutated(trait, mutate):
    out = type(trait)()
    out.CopyFrom(trait)
    mutate(out)
    return out


def app_changes(capture):
    cache = TraitCache()
    for raw in codec.parse_stream_body(capture(RECORDING)):
        for ch in cache.apply_frame(codec.ObserveFrame.FromString(raw)):
            if ch.resource_id == THERMOSTAT1 and ch.state_type == CONFIRMED and not ch.is_new:
                yield ch.label, ch.old.message, ch.new.message


def mutations_for(label, old, new):
    """Our mutations matching what changed in one app write."""
    out = []
    for name, (flag_label, field) in SETTING_FLAGS.items():
        if flag_label == label and getattr(old, field) != getattr(new, field):
            out.append((name, set_flag(name, getattr(new, field))))
    for name, spec in THRESHOLDS.items():
        if spec.label == label and getattr(old, spec.field) != getattr(new, spec.field):
            t = getattr(new, spec.field)
            out.append((name, set_threshold(name, t.value.value if t.enabled else None)))
    if label == "preconditioning_settings":
        hours = new.maxNighttimeSeconds.seconds // 3600 if new.preconditioningEnabled else None
        out.append(("early_on", set_early_on(hours)))
    if label == "display_settings" and old.temperatureScale != new.temperatureScale:
        out.append(("units", set_temperature_scale(SCALE.TemperatureScale.Name(new.temperatureScale)[-1])))
    if label == "remote_comfort_sensing_settings":
        before = {s.deviceId.resourceId for s in old.associatedRcsSensors}
        after = {s.deviceId.resourceId for s in new.associatedRcsSensors}
        out += [("pair", pair_sensor(s)) for s in after - before]
        out += [("unpair", unpair_sensor(s)) for s in before - after]
    return out


def test_every_app_settings_change_is_reproduced(capture):
    seen = []
    for label, old, new in app_changes(capture):
        found = mutations_for(label, old, new)
        if not found:
            continue  # readings, or side effects such as schedule re-keying
        result = old
        for name, mutate in found:
            result = mutated(result, mutate)
            seen.append(name)
        assert result == new, f"{label}: {[n for n, _ in found]}"
    assert set(seen) == {
        "auto_schedule", "home_away_assist", "early_on", "cool_to_dry", "sunblock", "airwave", "wake_on_approach",
        "eco_heat", "eco_cool", "safety_low", "safety_high", "units", "pair", "unpair",
    }


# --- Validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "celsius"),
    [
        ("eco_heat", 4.0),  # 39°F: below the range ("off" is its own choice)
        ("eco_heat", 22.0),  # above 70°F
        ("eco_cool", 32.3),  # 90°F is the "off" position
        ("safety_low", 1.5),
        ("safety_high", 41.0),
        ("eco_cool", float("nan")),
    ],
)
def test_threshold_outside_app_range_is_refused(name, celsius):
    with pytest.raises(ValueError):
        set_threshold(name, celsius)


def test_threshold_off_parks_value_at_off_position():
    t = hvac_pb2.SafetyTemperatureSettingsTrait()
    t.lowerSafetyTemp.enabled = True
    t.lowerSafetyTemp.value.value = 4.46
    off = mutated(t, set_threshold("safety_low", None))
    assert not off.lowerSafetyTemp.enabled
    assert off.lowerSafetyTemp.value.value == pytest.approx(1.6666667)  # 35°F
    assert mutated(off, set_threshold("safety_low", None)) == off  # already off: no-op


def test_early_on_rejects_out_of_range_hours():
    with pytest.raises(ValueError):
        set_early_on(6)


def rcs_with(active=None, scheduled=None, paired=()):
    t = hvac_pb2.RemoteComfortSensingSettingsTrait()
    for sid in paired:
        t.associatedRcsSensors.add(vendorId=9050, productId=26).deviceId.resourceId = sid
    if active:
        t.activeRcsSelection.activeRcsSensor.resourceId = active
    if scheduled:
        t.rcsControlSchedule.intervals[0].rcsSelection.activeRcsSensor.resourceId = scheduled
    return t


def test_unpair_refuses_active_or_scheduled_sensor():
    with pytest.raises(ValueError, match="active"):
        mutated(rcs_with(active=ROOM3, paired=[ROOM3]), unpair_sensor(ROOM3))
    with pytest.raises(ValueError, match="schedule"):
        mutated(rcs_with(scheduled=ROOM3, paired=[ROOM3]), unpair_sensor(ROOM3))


def test_pair_is_idempotent_and_unpair_keeps_others():
    t = rcs_with(paired=["DEVICE_A"])
    t = mutated(t, pair_sensor(ROOM3))
    assert mutated(t, pair_sensor(ROOM3)) == t
    t = mutated(t, unpair_sensor(ROOM3))
    assert [s.deviceId.resourceId for s in t.associatedRcsSensors] == ["DEVICE_A"]


# --- Humidifier, humidity control, Farsight (app session 2, same recording) --------

from pynestproto.write import (  # noqa: E402
    DISPLAY_FARSIGHT,
    HUMIDIFIER_TYPES,
    set_farsight,
    set_humidifier,
    set_humidity_control,
)

EQ = hvac_pb2.EquipmentSettingsTrait


def test_app_humidifier_display_and_humidity_control_writes_reproduced(capture):
    kinds = set()
    by_value = {v: k for k, v in DISPLAY_FARSIGHT.items()}
    types = {v: k for k, v in HUMIDIFIER_TYPES.items()}
    for label, old, new in app_changes(capture):
        if label == "equipment_settings":
            mutate = set_humidifier(
                kind=types[new.humidifierType] if new.humidifierType != old.humidifierType else None,
                run_fan=(new.humidifierFanActivation == EQ.HUMIDIFIER_FAN_TYPE_ACTIVATE)
                if new.humidifierFanActivation != old.humidifierFanActivation else None,
            )
            kinds.add("humidifier")
        elif label == "humidity_control_settings" and old.targetHumidity.enabled != new.targetHumidity.enabled:
            mutate = set_humidity_control(new.targetHumidity.enabled)
            kinds.add("humidity_control")
        elif label == "display_settings" and old.farsightDisplay != new.farsightDisplay:
            mutate = set_farsight(by_value[new.farsightDisplay])
            kinds.add("farsight")
        else:
            continue
        # equipment_settings carries the whole installer configuration: equality
        # proves nothing else in it is touched.
        assert mutated(old, mutate) == new
    assert kinds == {"humidifier", "humidity_control", "farsight"}
