"""Deliver the shipped cards through Home Assistant's Lovelace resources."""

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
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
    """Serve the card directory and register its native dashboard resource.

    Runs once per Home Assistant start from ``async_setup``. Failure is logged
    and never affects native Proxmox or package functionality.
    """
    lovelace = hass.data.get(LOVELACE_DATA)
    if (
        hass.http is None
        or "frontend" not in hass.config.components
        or lovelace is None
    ):
        _LOGGER.debug(
            "Dashboard frontend is not loaded; Hubinet-Ops card not delivered"
        )
        return
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(FRONTEND_DIRECTORY), True)]
        )
        resources = lovelace.resources
        url = card_module_url()
        if not isinstance(resources, ResourceStorageCollection):
            if not any(
                item["url"] == url and item["type"] == "module"
                for item in resources.async_items()
            ):
                _LOGGER.warning(
                    "Lovelace uses YAML resources; add URL %s with type module", url
                )
            return

        # Let HA load/migrate its native collection before inspecting it. Never
        # edit .storage or dashboard configuration directly.
        await resources.async_get_info()
        card_path = f"{STATIC_URL}/{CARD_MODULE}"
        existing = [
            item
            for item in resources.async_items()
            if item["url"].partition("?")[0].partition("#")[0] == card_path
        ]
        data = {"url": url, "res_type": "module"}
        if not existing:
            await resources.async_create_item(data)
            return

        first, *duplicates = existing
        if first["url"] != url or first["type"] != "module":
            await resources.async_update_item(first["id"], data)
        for duplicate in duplicates:
            await resources.async_delete_item(duplicate["id"])
    except Exception:
        _LOGGER.exception("Could not deliver the Hubinet-Ops dashboard card")
