"""Thin Home Assistant orchestration over existing package scan entry points."""

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback

from .const import DOMAIN, VM_CONTAINER_RUNNING
from .packages.models import PackageScanError

_LOGGER = logging.getLogger(__name__)
SERVICE_SCAN_ALL_PACKAGES = "scan_all_packages"


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register one targetless Scan All action for currently loaded entries."""

    @callback
    def scan_all_packages(_call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            coordinator = entry.runtime_data
            manager = coordinator.package_manager
            if not manager.configured or not coordinator.last_update_success:
                continue
            node_data = coordinator.data.get(coordinator.package_node)
            if node_data is None:
                continue
            node = node_data.node["node"]
            for vmid, container in node_data.containers.items():
                try:
                    manager.async_start_scan(
                        node,
                        vmid,
                        target_is_running=container.get("status")
                        == VM_CONTAINER_RUNNING,
                    )
                except PackageScanError as err:
                    _LOGGER.debug(
                        "Scan All did not start %s/%s: %s", node, vmid, err.failure
                    )
                except Exception:
                    _LOGGER.exception("Scan All could not request %s/%s", node, vmid)

    hass.services.async_register(
        DOMAIN, SERVICE_SCAN_ALL_PACKAGES, scan_all_packages, schema=vol.Schema({})
    )
