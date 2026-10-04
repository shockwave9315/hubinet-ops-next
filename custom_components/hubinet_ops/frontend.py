"""Deliver the shipped cards through Home Assistant's Lovelace resources."""

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, INTEGRATION_VERSION

_LOGGER = logging.getLogger(__name__)
STATIC_URL = "/hubinet_ops_static"
CARD_MODULE = "hubinet-ops-cards.js"
FRONTEND_DIRECTORY = Path(__file__).parent / "frontend"
_STATIC_REGISTERED: HassKey[bool] = HassKey("hubinet_ops_frontend_static_registered")


def card_module_url() -> str:
    """Return the versioned module URL so browsers fetch each release once."""
    return f"{STATIC_URL}/{CARD_MODULE}?v={INTEGRATION_VERSION}"


def _is_card_resource(url: str) -> bool:
    """Own only this exact relative path, independent of its release query."""
    return url.partition("?")[0].partition("#")[0] == f"{STATIC_URL}/{CARD_MODULE}"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the card directory and register its native dashboard resource.

    Integration and entry setup share this idempotent registration. The HTTP
    route is served once per HA runtime; a resource removed with the last host
    is restored when a host is added again. Failure never disables PVE.
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
        if not hass.data.get(_STATIC_REGISTERED):
            await hass.http.async_register_static_paths(
                [StaticPathConfig(STATIC_URL, str(FRONTEND_DIRECTORY), True)]
            )
            hass.data[_STATIC_REGISTERED] = True
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
        existing = [
            item for item in resources.async_items() if _is_card_resource(item["url"])
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


async def async_remove_frontend_resource(hass: HomeAssistant) -> None:
    """Remove the owned resource only after permanent removal of the last host."""
    if hass.config_entries.async_entries(DOMAIN, include_ignore=False):
        return
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None or not isinstance(
        resources := lovelace.resources, ResourceStorageCollection
    ):
        return
    try:
        await resources.async_get_info()
        for item in list(resources.async_items()):
            # A host can be added while the collection loads or a native
            # deletion notifies its listeners. Disabled/not-loaded hosts count.
            if hass.config_entries.async_entries(DOMAIN, include_ignore=False):
                return
            if _is_card_resource(item["url"]) and any(
                current["id"] == item["id"] for current in resources.async_items()
            ):
                await resources.async_delete_item(item["id"])
    except Exception:
        _LOGGER.exception("Could not remove the Hubinet-Ops dashboard resource")
