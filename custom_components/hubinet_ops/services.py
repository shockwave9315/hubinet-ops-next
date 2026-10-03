"""Thin Home Assistant orchestration over existing package scan entry points."""

import logging

import voluptuous as vol

from homeassistant.auth.models import User
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN, VM_CONTAINER_RUNNING
from .easy_update import (
    EasyUpdateTarget,
    async_resolve_target,
    async_start_easy_update,
    async_start_post_update_autoremove,
)
from .packages.models import PackageScanError

_LOGGER = logging.getLogger(__name__)
SERVICE_SCAN_ALL_PACKAGES = "scan_all_packages"
SERVICE_EASY_UPDATE = "easy_update"
ATTR_DEVICE_ID = "device_id"
ATTR_AUTOREMOVE = "autoremove"
ATTR_EXPECTED_SCAN_ATTEMPT = "expected_scan_attempt"
EASY_UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_AUTOREMOVE, default=False): cv.boolean,
        vol.Optional(ATTR_EXPECTED_SCAN_ATTEMPT): cv.string,
    }
)


@callback
def async_scan_entry(entry: ConfigEntry) -> None:
    """Request a Scan for every package LXC of one loaded entry."""
    coordinator = entry.runtime_data
    manager = coordinator.package_manager
    if not manager.configured or not coordinator.last_update_success:
        return
    node_data = coordinator.data.get(coordinator.package_node)
    if node_data is None:
        return
    node = node_data.node["node"]
    for vmid, container in node_data.containers.items():
        try:
            manager.async_start_scan(
                node,
                vmid,
                target_is_running=container.get("status") == VM_CONTAINER_RUNNING,
            )
        except PackageScanError as err:
            _LOGGER.debug("Scan did not start %s/%s: %s", node, vmid, err.failure)
        except Exception:
            _LOGGER.exception("Scan could not request %s/%s", node, vmid)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register Scan All and the device-targeted Easy Update action."""

    @callback
    def scan_all_packages(_call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                async_scan_entry(entry)

    hass.services.async_register(
        DOMAIN, SERVICE_SCAN_ALL_PACKAGES, scan_all_packages, schema=vol.Schema({})
    )

    async def easy_update(call: ServiceCall) -> None:
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context)
            if not user.is_admin:
                # Same entity CONTROL a user needs to press these buttons.
                _verify_control(
                    hass,
                    call,
                    user,
                    async_resolve_target(hass, call.data[ATTR_DEVICE_ID]),
                    ("package_update", "package_autoremove")
                    if call.data[ATTR_AUTOREMOVE]
                    else ("package_update",),
                )
        target, update = async_start_easy_update(
            hass,
            call.data[ATTR_DEVICE_ID],
            expected_scan_attempt=call.data.get(ATTR_EXPECTED_SCAN_ATTEMPT),
        )
        if call.data[ATTR_AUTOREMOVE]:
            async_start_post_update_autoremove(hass, target, update)

    hass.services.async_register(
        DOMAIN, SERVICE_EASY_UPDATE, easy_update, schema=EASY_UPDATE_SCHEMA
    )


def _verify_control(
    hass: HomeAssistant,
    call: ServiceCall,
    user: User,
    target: EasyUpdateTarget,
    keys: tuple[str, ...],
) -> None:
    """Require CONTROL on the target's existing Update/Autoremove buttons."""
    registry = er.async_get(hass)
    for key in keys:
        # The stable unique ID of the upstream-derived container button.
        entity_id = registry.async_get_entity_id(
            "button", DOMAIN, f"{target.entry.entry_id}_{target.vmid}_{key}"
        )
        if entity_id is None or not user.permissions.check_entity(
            entity_id, POLICY_CONTROL
        ):
            raise Unauthorized(
                context=call.context,
                entity_id=entity_id,
                permission=POLICY_CONTROL,
            )
