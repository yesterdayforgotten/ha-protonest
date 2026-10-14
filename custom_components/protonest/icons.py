"""Load ProtoNest's "protonest:" icon set into the Home Assistant frontend."""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_SCRIPT = Path(__file__).parent / "frontend" / "protonest-icons.js"
_URL = f"/{DOMAIN}/protonest-icons.js"


async def async_register_icons(hass: HomeAssistant) -> None:
    # Only where the frontend runs (not, for example, in tests).
    if hass.http is None or "frontend" not in hass.config.components:
        return
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    await hass.http.async_register_static_paths([StaticPathConfig(_URL, str(_SCRIPT), True)])
    # The version busts the browser's cache of the script after an update.
    version = (await async_get_integration(hass, DOMAIN)).version
    add_extra_js_url(hass, f"{_URL}?v={version}")
