"""Deliver the integration-shipped dashboard card without a manual resource."""

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import INTEGRATION_VERSION

_LOGGER = logging.getLogger(__name__)
STATIC_URL = "/hubinet_ops_static"
CARD_MODULE = "hubinet-ops-cards.js"
FRONTEND_DIRECTORY = Path(__file__).parent / "frontend"


def card_module_url() -> str:
    """Return the versioned module URL so browsers fetch each release once."""
    return f"{STATIC_URL}/{CARD_MODULE}?v={INTEGRATION_VERSION}"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the card directory and load its module on every frontend page.

    Runs once per Home Assistant start from ``async_setup``. Failure is logged
    and never affects native Proxmox or package functionality.
    """
    if hass.http is None or "frontend" not in hass.config.components:
        _LOGGER.debug("Frontend is not loaded; Hubinet-Ops card not delivered")
        return
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(FRONTEND_DIRECTORY), True)]
        )
        add_extra_js_url(hass, card_module_url())
    except Exception:
        _LOGGER.exception("Could not deliver the Hubinet-Ops dashboard card")
