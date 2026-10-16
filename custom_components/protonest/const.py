"""Constants for the ProtoNest integration."""

from homeassistant.const import Platform

DOMAIN = "protonest"

CONF_ISSUE_TOKEN = "issue_token"
CONF_COOKIES = "cookies"
CONF_NOTIFY = "notify"  # option: notify services that also get the sign-in alert

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
