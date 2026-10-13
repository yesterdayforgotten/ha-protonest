"""Constants for the ProtoNest integration."""

from homeassistant.const import Platform

DOMAIN = "protonest"

CONF_ISSUE_TOKEN = "issue_token"
CONF_COOKIES = "cookies"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
