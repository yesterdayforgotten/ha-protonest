"""ProtoNest's frontend files: the "protonest:" icon set and the schedule card."""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_DIR = Path(__file__).parent / "frontend"
_URL = f"/{DOMAIN}"
_MODULES = ("protonest-icons.js", "protonest-schedule-card.js")


async def async_register_frontend(hass: HomeAssistant) -> None:
    # Only where the frontend runs (not, for example, in tests).
    if hass.http is None or "frontend" not in hass.config.components:
        return
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    await hass.http.async_register_static_paths([StaticPathConfig(_URL, str(_DIR), True)])
    # The version busts the browser's cache of the files after an update.
    version = (await async_get_integration(hass, DOMAIN)).version
    for module in _MODULES:
        add_extra_js_url(hass, f"{_URL}/{module}?v={version}")
