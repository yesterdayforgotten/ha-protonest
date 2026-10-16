"""Home (structure) settings and the newer read-only thermostat values."""

from pynestproto import codec
from pynestproto.model import Structure, Thermostat
from pynestproto.observe import ACCEPTED, CONFIRMED, TraitCache
from pynestproto.proto_gen.nest.trait import hvac_pb2, network_pb2, occupancy_pb2
from pynestproto.proto_gen.nestlabs.gateway import v2_pb2
from pynestproto.write import set_hvac_alerts

HOME = "STRUCTURE_0000000000000D01"
DEVICE = "DEVICE_0000000000000A01"


def mutated(trait, mutate):
    out = type(trait)()
    out.CopyFrom(trait)
    mutate(out)
    return out


def cache_with(resource_id, label, trait, state=CONFIRMED) -> TraitCache:
    cache = TraitCache()
    cache.apply_frame(codec.ObserveFrame(traitStates=[v2_pb2.TraitState(
        traitId=v2_pb2.TraitId(resourceId=resource_id, traitLabel=label), stateTypes=[state],
        patch=v2_pb2.Patch(values=codec.pack(trait)), monotonicVersion=1)]))
    return cache


def test_hvac_alerts():
    trait = hvac_pb2.HvacDiagnosticsAlertsSettingsTrait()
    trait.enableAlerts.value = True
    trait.lastAlertTime.FromSeconds(1_700_000_000)
    off = mutated(trait, set_hvac_alerts(False))
    assert off.HasField("enableAlerts") and off.enableAlerts.value is False
    assert off.lastAlertTime == trait.lastAlertTime


def test_structure_mode_and_alerts():
    mode = occupancy_pb2.StructureModeTrait(structureMode=occupancy_pb2.StructureModeTrait.STRUCTURE_MODE_AWAY)
    assert Structure(cache_with(HOME, "structure_mode", mode, ACCEPTED), HOME).mode == "away"
    alerts = hvac_pb2.HvacDiagnosticsAlertsSettingsTrait()
    home = Structure(cache_with(HOME, "hvac_diagnostics_alerts_settings", alerts, ACCEPTED), HOME)
    assert home.hvac_alerts is None and home.last_hvac_alert is None
    alerts.enableAlerts.value = False
    alerts.lastAlertTime.FromSeconds(1_700_000_000)
    home = Structure(cache_with(HOME, "hvac_diagnostics_alerts_settings", alerts, ACCEPTED), HOME)
    assert home.hvac_alerts is False and home.last_hvac_alert.timestamp() == 1_700_000_000


def test_wifi_signal_is_negative_dbm():
    wifi = network_pb2.WifiInterfaceTrait(rssi=77)
    assert Thermostat(cache_with(DEVICE, "wifi_interface", wifi), DEVICE).wifi_signal == -77
    assert Thermostat(cache_with(DEVICE, "wifi_interface", network_pb2.WifiInterfaceTrait()), DEVICE).wifi_signal is None


def test_seasonal_savings_state():
    ss = hvac_pb2.SeasonalSavingsTrait
    t = lambda state: Thermostat(cache_with(DEVICE, "seasonal_savings", ss(state=state)), DEVICE)  # noqa: E731
    assert t(ss.SEASONAL_SAVINGS_STATE_FINISHED).seasonal_savings == "finished"
    assert t(ss.SEASONAL_SAVINGS_STATE_PRESENTING_EVENT).seasonal_savings == "offered"
    assert t(ss.SEASONAL_SAVINGS_STATE_UNSPECIFIED).seasonal_savings is None
