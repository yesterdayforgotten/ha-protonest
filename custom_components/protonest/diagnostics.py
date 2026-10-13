"""Diagnostics download: connection status and the raw trait state, without credentials."""

from __future__ import annotations

from typing import Any

from google.protobuf import json_format
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_COOKIES, CONF_ISSUE_TOKEN
from .coordinator import ProtoNestConfigEntry
from .pynestproto.observe import ACCEPTED, CONFIRMED

TO_REDACT = {CONF_ISSUE_TOKEN, CONF_COOKIES, "pinHash", "serialNumber", "weaveNodeId", "fabricId", "hgDeviceId"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ProtoNestConfigEntry) -> dict[str, Any]:
    client = entry.runtime_data.client
    cache = client.cache
    devices: dict[str, Any] = {}
    for rid, rtype in cache.resource_types.items():
        traits = {}
        for label in sorted(cache.labels(rid)):
            for kind, name in ((CONFIRMED, "confirmed"), (ACCEPTED, "accepted")):
                msg = cache.get(rid, label, kind)
                if msg is not None:
                    traits.setdefault(label, {})[name] = json_format.MessageToDict(msg)
        devices[rid] = {"name": cache.display_name(rid), "type": rtype, "traits": traits}
    return async_redact_data(
        {
            "entry": dict(entry.data),
            "status": {
                "ready": client.ready,
                "connected": client.status.connected,
                "auth_failed": client.status.auth_failed,
                "last_error": client.status.last_error,
            },
            "devices": devices,
        },
        TO_REDACT,
    )
