"""Identifiers of the home the recorded captures come from.

The real values are personal, so they live in the gitignored
captures/home.json next to the recordings that contain them. Without it, these
are placeholders: tests built from synthetic data still run, and tests that
compare against real recordings skip because the recordings are missing too.
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "captures" / "home.json"

_PLACEHOLDERS = {
    "thermostat1": "DEVICE_0000000000000A01",
    "thermostat2": "DEVICE_0000000000000A02",
    "thermostat3": "DEVICE_0000000000000A03",
    "room1": "DEVICE_0000000000000B01",
    "room2": "DEVICE_0000000000000B02",
    "room3": "DEVICE_0000000000000B03",
    "room4": "DEVICE_0000000000000B04",
    "user": "USER_0000000000000C01",
    "user_id": "123456",
    "thermostat1_serial": "SERIAL0000000001",
    "thermostat1_name": "Thermostat 1",
    "thermostat2_name": "Thermostat 2",
    "thermostat3_name": "Thermostat 3",
    "room1_name": "Room 1",
    "room2_name": "Room 2",
    "room3_name": "Room 3",
    "room4_name": "Room 4",
    "room5_name": "Room 5",
}

_values = {**_PLACEHOLDERS, **(json.loads(_PATH.read_text()) if _PATH.exists() else {})}

THERMOSTAT1 = _values["thermostat1"]
THERMOSTAT2 = _values["thermostat2"]
THERMOSTAT3 = _values["thermostat3"]
THERMOSTAT1_NAME = _values["thermostat1_name"]
THERMOSTAT2_NAME = _values["thermostat2_name"]
THERMOSTAT3_NAME = _values["thermostat3_name"]
ROOM1 = _values["room1"]
ROOM2 = _values["room2"]
ROOM3 = _values["room3"]
ROOM4 = _values["room4"]
USER = _values["user"]
USER_ID = _values["user_id"]
THERMOSTAT1_SERIAL = _values["thermostat1_serial"]
ROOM1_NAME = _values["room1_name"]
ROOM2_NAME = _values["room2_name"]
ROOM3_NAME = _values["room3_name"]
ROOM4_NAME = _values["room4_name"]
ROOM5_NAME = _values["room5_name"]
