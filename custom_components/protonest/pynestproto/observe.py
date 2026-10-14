"""Observe stream state: what traits to subscribe to, and a cache of their values.

Every trait state arrives as ACCEPTED (the server accepted a change, not yet
acknowledged by the device) and/or CONFIRMED (the device reports it), each with
its own ``monotonicVersion``. The cache keeps the latest of each kind and
drops stale or duplicate updates. Updates are treated as the complete trait
value, replacing the previous one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from google.protobuf import message as pb_message

from . import codec
from .proto_gen.nest.trait import (
    detector_pb2,
    firmware_pb2,
    hvac_pb2,
    located_pb2,
    network_pb2,
    occupancy_pb2,
    sensor_pb2,
    service_pb2,
    structure_pb2,
    user_pb2,
)
from .proto_gen.nestlabs.gateway import v2_pb2
from .proto_gen.weave.trait import description_pb2, heartbeat_pb2, power_pb2

CONFIRMED = v2_pb2.StateType.CONFIRMED
ACCEPTED = v2_pb2.StateType.ACCEPTED

# Traits needed for thermostats, their remote sensors, and naming.
OBSERVED_TRAITS: tuple[type[pb_message.Message], ...] = (
    # Thermostat control and state
    hvac_pb2.TargetTemperatureSettingsTrait,
    hvac_pb2.HvacControlTrait,
    hvac_pb2.HvacEquipmentCapabilitiesTrait,
    hvac_pb2.EquipmentSettingsTrait,
    hvac_pb2.FanControlTrait,
    hvac_pb2.FanControlSettingsTrait,
    hvac_pb2.FanControlCapabilitiesTrait,
    hvac_pb2.HumidityControlSettingsTrait,
    hvac_pb2.EcoModeTrait,
    hvac_pb2.EcoModeStateTrait,
    hvac_pb2.EcoModeSettingsTrait,
    hvac_pb2.DisplaySettingsTrait,
    hvac_pb2.TemperatureLockSettingsTrait,
    hvac_pb2.RemoteComfortSensingSettingsTrait,
    hvac_pb2.LeafTrait,
    # Thermostat settings
    hvac_pb2.ScheduleLearningSettingsTrait,
    hvac_pb2.PreconditioningSettingsTrait,
    hvac_pb2.CoolToDrySettingsTrait,
    hvac_pb2.SunblockSettingsTrait,
    hvac_pb2.AirwaveSettingsTrait,
    hvac_pb2.SafetyTemperatureSettingsTrait,
    hvac_pb2.WakeOnApproachSettingsTrait,
    hvac_pb2.FilterReminderSettingsTrait,
    hvac_pb2.SetPointScheduleSettingsTrait,  # heat/cool/range weekly schedules (by label)
    hvac_pb2.UtilitySettingsTrait,
    hvac_pb2.HvacDiagnosticsAlertsSettingsTrait,
    hvac_pb2.SeasonalSavingsSettingsTrait,
    occupancy_pb2.StructureModeSettingsTrait,  # sleep schedule
    # Read-only state
    hvac_pb2.RemoteComfortSensingStateTrait,  # per-sensor insights, multi-sensor reading
    hvac_pb2.CoolToDryTrait,
    hvac_pb2.FilterReminderTrait,
    hvac_pb2.TimeToTemperatureTrait,
    hvac_pb2.SeasonalSavingsTrait,
    hvac_pb2.WiringTrait,
    sensor_pb2.BatteryVoltageTrait,  # the thermostat's own battery
    detector_pb2.AmbientMotionTrait,
    firmware_pb2.FirmwareTrait,
    network_pb2.RadioSignalStrengthTrait,
    # Sensors (thermostat and KR1 remote sensors)
    sensor_pb2.TemperatureTrait,
    sensor_pb2.HumidityTrait,
    power_pb2.BatteryPowerSourceTrait,
    # Identity, liveness, naming
    description_pb2.DeviceIdentityTrait,
    description_pb2.LabelSettingsTrait,
    heartbeat_pb2.LivenessTrait,
    service_pb2.DeviceInfoTrait,
    located_pb2.DeviceLocatedSettingsTrait,
    located_pb2.LocatedAnnotationsTrait,
    located_pb2.CustomLocatedAnnotationsTrait,
    # Structure
    structure_pb2.StructureInfoTrait,
    occupancy_pb2.StructureModeTrait,
    # Brings in our own USER_ resource; writes name it as the originator.
    user_pb2.UserInfoTrait,
)

USER_RESOURCE = "nest.resource.user.UserResource"


def build_observe_request(
    trait_types: Iterable[str] | None = None,
    resource_ids: Iterable[str] = (),
) -> v2_pb2.ObserveRequest:
    """Build an ObserveRequest for the given fully qualified trait type names."""
    if trait_types is None:
        trait_types = (t.DESCRIPTOR.full_name for t in OBSERVED_TRAITS)
    return v2_pb2.ObserveRequest(
        stateTypes=[ACCEPTED, CONFIRMED],
        resourceIds=list(resource_ids),
        traitTypeParams=[v2_pb2.TraitTypeObserveParams(traitType=t) for t in trait_types],
    )


@dataclass(frozen=True)
class TraitValue:
    type_name: str
    version: int
    message: pb_message.Message | None  # None if the type isn't in our schema


@dataclass(frozen=True)
class TraitChange:
    resource_id: str
    label: str
    state_type: int  # CONFIRMED or ACCEPTED
    old: TraitValue | None
    new: TraitValue

    @property
    def is_new(self) -> bool:
        return self.old is None


class TraitCache:
    """Latest ACCEPTED and CONFIRMED value of every (resource, trait label)."""

    def __init__(self) -> None:
        self.resource_types: dict[str, str] = {}
        self._traits: dict[tuple[str, str, int], TraitValue] = {}
        # ACCEPTED values not yet confirmed, oldest first, per (resource, label): see latest().
        self._unconfirmed: dict[tuple[str, str], list[pb_message.Message]] = {}

    def apply_frame(self, frame: codec.ObserveFrame) -> list[TraitChange]:
        """Apply one Observe frame; return the trait values that changed."""
        for meta in frame.resourceMetas:
            if meta.status == v2_pb2.ResourceStatus.REMOVED:
                self.remove_resource(meta.resourceId)
            else:
                self.resource_types[meta.resourceId] = meta.type

        changes: list[TraitChange] = []
        for ts in frame.traitStates:
            rid, label = ts.traitId.resourceId, ts.traitId.traitLabel
            new = TraitValue(
                type_name=codec.trait_type_name(ts.patch.values),
                version=ts.monotonicVersion,
                message=codec.unpack(ts.patch.values),
            )
            for state_type in ts.stateTypes:
                key = (rid, label, state_type)
                old = self._traits.get(key)
                if old is not None and new.version <= old.version:
                    continue  # stale or duplicate
                self._traits[key] = new
                pair = (rid, label)
                if state_type == ACCEPTED:
                    self._unconfirmed.setdefault(pair, []).append(new.message)
                    del self._unconfirmed[pair][:-16]
                else:
                    accepted = self._traits.get((rid, label, ACCEPTED))
                    if accepted is not None and accepted.message == new.message:
                        self._unconfirmed.pop(pair, None)  # caught up
                if old is None or old.message != new.message:
                    changes.append(TraitChange(rid, label, state_type, old, new))
        return changes

    def remove_resource(self, resource_id: str) -> None:
        self.resource_types.pop(resource_id, None)
        for key in [k for k in self._traits if k[0] == resource_id]:
            del self._traits[key]
        for pair in [k for k in self._unconfirmed if k[0] == resource_id]:
            del self._unconfirmed[pair]

    def get(
        self, resource_id: str, label: str, state_type: int = CONFIRMED
    ) -> pb_message.Message | None:
        value = self._traits.get((resource_id, label, state_type))
        return value.message if value else None

    def latest(self, resource_id: str, label: str) -> TraitValue | None:
        """The newest value of a trait, whether ACCEPTED or CONFIRMED.

        Writes must start from this: a pending (ACCEPTED, not yet CONFIRMED)
        change is newer than the CONFIRMED value, and building on the
        CONFIRMED one would undo it.
        """
        confirmed = self._traits.get((resource_id, label, CONFIRMED))
        accepted = self._traits.get((resource_id, label, ACCEPTED))
        # After quick writes, the device's confirmation of an earlier one can carry a
        # newer version than the server's acceptance of the last one: it is older all
        # the same. Seen live (2026-10-14) as a schedule flicking back for a moment.
        if (
            confirmed is not None and accepted is not None
            and confirmed.version > accepted.version
            and confirmed.message != accepted.message
            and any(confirmed.message == m for m in self._unconfirmed.get((resource_id, label), ()))
        ):
            return accepted
        values = [v for v in (confirmed, accepted) if v is not None]
        return max(values, key=lambda v: v.version, default=None)

    def user_resource_id(self) -> str | None:
        """Our own USER_ resource ID, if the stream has delivered it."""
        return next(
            (rid for rid, t in self.resource_types.items() if t == USER_RESOURCE), None
        )

    def labels(self, resource_id: str) -> set[str]:
        return {k[1] for k in self._traits if k[0] == resource_id}

    def where_names(self) -> dict[str, str]:
        """Map where-annotation IDs (ANNOTATION_...) to room names."""
        names: dict[str, str] = {}
        for value in self._traits.values():
            msg = value.message
            if msg is None or not value.type_name.endswith("LocatedAnnotationsTrait"):
                continue
            for field, container in msg.ListFields():
                if field.message_type is None or not field.message_type.GetOptions().map_entry:
                    continue
                for item in container.values():
                    if item.HasField("whereId") and item.HasField("label"):
                        names[item.whereId.resourceId] = item.label.literal
        return names

    def display_name(self, resource_id: str) -> str:
        """Best human-readable name for a resource: its label, else its room."""
        label = self.get(resource_id, "label") or self.get(resource_id, "label", ACCEPTED)
        if label is not None and label.label:
            return label.label
        located = self.get(resource_id, "device_located_settings") or self.get(
            resource_id, "device_located_settings", ACCEPTED
        )
        if located is not None and located.HasField("whereAnnotationRid"):
            where = self.where_names().get(located.whereAnnotationRid.resourceId)
            if where:
                return where
        return resource_id
