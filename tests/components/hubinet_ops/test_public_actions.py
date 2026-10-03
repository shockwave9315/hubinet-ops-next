"""The public Hubinet-Ops actions and the package-LXC picker metadata."""

import asyncio
from unittest.mock import patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from homeassistant.components.button import SERVICE_PRESS, ButtonDeviceClass
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import setup_integration
from .test_packages import GateTransport

pytestmark = pytest.mark.usefixtures("mock_proxmox_client")

CT_NGINX_SENSOR = "sensor.ct_nginx_pending_package_updates"
CT_NGINX_BUTTON = "button.ct_nginx_scan_pending_packages"


async def test_only_scan_all_and_easy_update_are_published(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The retired package-review actions are gone; the two others remain."""
    await setup_integration(hass, mock_config_entry)
    assert set(hass.services.async_services_for_domain(DOMAIN)) == {
        "scan_all_packages",
        "easy_update",
    }
    assert not hass.services.has_service(DOMAIN, "get_package_plan")
    assert not hass.services.has_service(DOMAIN, "confirm_package_review")


async def test_token_and_packages_are_absent_from_entity_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Entity attributes carry `reviewed`, never the scan token or rows."""
    transport = GateTransport()
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=lambda _self, node, vmid: transport.async_scan(node, vmid),
    ):
        await setup_integration(hass, mock_config_entry)
        await hass.services.async_call(
            "button", SERVICE_PRESS, {ATTR_ENTITY_ID: CT_NGINX_BUTTON}, blocking=True
        )
        await asyncio.wait_for(transport.entered(200).wait(), 1)
        transport.release(200)
        await asyncio.wait_for(transport.completed(200).wait(), 1)
        await hass.async_block_till_done()

    state = hass.states.get(CT_NGINX_SENSOR)
    assert state is not None
    assert state.attributes["scan_status"] == "success"
    assert "reviewed" in state.attributes
    assert "token" not in state.attributes
    assert "packages" not in state.attributes
    assert "supported_features" not in state.attributes


async def test_update_class_marks_exactly_the_package_lxcs(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The Easy Update picker filter matches only package LXC devices."""
    await setup_integration(hass, mock_config_entry)
    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    update_devices = {
        entry.device_id
        for entry in entries
        if entry.domain == "button"
        and entry.original_device_class == ButtonDeviceClass.UPDATE
    }
    package_devices = {
        entry.device_id
        for entry in entries
        if entry.translation_key == "pending_packages"
    }
    assert package_devices
    assert update_devices == package_devices
    assert all(
        entry.translation_key == "package_update"
        for entry in entries
        if entry.original_device_class == ButtonDeviceClass.UPDATE
    )
