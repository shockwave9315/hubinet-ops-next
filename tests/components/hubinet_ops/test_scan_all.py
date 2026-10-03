"""Exercise Scan All through the real service and existing PackageManagers."""

# ruff: noqa: SLF001 -- verify existing ephemeral state and fixed shipped sources

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import (
    CONF_PACKAGE_NODE,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PRIVATE_KEY,
    DOMAIN,
)
from custom_components.hubinet_ops.coordinator import ProxmoxNodeData
from custom_components.hubinet_ops.packages.models import (
    HealthCheckStatus,
    PackageHealthRecord,
    PackageScanRecord,
    PackageScanStatus,
    PackageUpdateRecord,
    PackageUpdateStatus,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import setup_integration
from .test_packages import RESULT, GateTransport

pytestmark = pytest.mark.usefixtures("mock_proxmox_client")


def _add_container(coordinator, vmid):
    """Represent another currently discovered, running LXC on the package node."""
    coordinator.data["pve1"].containers[vmid] = {
        "vmid": str(vmid),
        "name": f"test-{vmid}",
        "status": "running",
    }


async def _scan_all(hass: HomeAssistant):
    await hass.services.async_call(DOMAIN, "scan_all_packages", blocking=True)


async def test_scan_all_scope_and_existing_entry_point(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    mock_proxmox_client: MagicMock,
) -> None:
    """Running package-node LXCs scan; stopped, other-node and QEMU targets do not."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    _add_container(coordinator, 300)
    coordinator.data["pve2"] = ProxmoxNodeData(
        node={"node": "pve2"},
        containers={400: {"vmid": "400", "status": "running"}},
    )
    manager = coordinator.package_manager
    press = AsyncMock()
    hass.services.async_register("button", "press", press)
    with (
        patch.object(
            manager, "async_start_scan", wraps=manager.async_start_scan
        ) as start,
        patch.object(
            manager._transport, "async_scan", AsyncMock(return_value=RESULT)
        ) as scan,
    ):
        await _scan_all(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert start.call_args_list == [
        call("pve1", 200, target_is_running=True),
        call("pve1", 201, target_is_running=False),
        call("pve1", 300, target_is_running=True),
    ]
    assert scan.await_args_list == [call("pve1", 200), call("pve1", 300)]
    press.assert_not_awaited()
    assert not any(
        args[0].endswith((".post", ".delete"))
        for args in mock_proxmox_client.mock_calls
    )
    assert manager.record("pve1", 201) == PackageScanRecord()
    assert manager.record("pve1", 300).status is PackageScanStatus.SUCCESS


@pytest.mark.parametrize("conflict", ["scan", "update", "cleanup", "health", "restore"])
async def test_scan_all_manager_conflict_does_not_stop_next_target(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    conflict: str,
) -> None:
    """Existing manager rules reject the busy LXC and still start its neighbour."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    _add_container(coordinator, 300)
    manager = coordinator.package_manager
    key = ("pve1", 200)
    if conflict == "scan":
        manager._records[key] = PackageScanRecord(status=PackageScanStatus.RUNNING)
    elif conflict == "update":
        manager._update_records[key] = PackageUpdateRecord(
            status=PackageUpdateStatus.RUNNING
        )
    elif conflict == "cleanup":
        manager._cleanup_records[key] = PackageUpdateRecord(
            status=PackageUpdateStatus.RUNNING
        )
    elif conflict == "health":
        manager._health_records[key] = PackageHealthRecord(
            check_status=HealthCheckStatus.RUNNING
        )
    else:
        assert manager.begin_restore(*key) is None
    with patch.object(
        manager._transport, "async_scan", AsyncMock(return_value=RESULT)
    ) as scan:
        await _scan_all(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    scan.assert_awaited_once_with("pve1", 300)


async def test_scan_all_unexpected_target_error_is_isolated(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A synchronous target failure cannot abort the remaining requests."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    _add_container(coordinator, 300)
    with patch.object(
        coordinator.package_manager,
        "async_start_scan",
        side_effect=[RuntimeError("one target failed"), None, None],
    ) as start:
        await _scan_all(hass)
    assert start.call_count == 3
    assert start.call_args == call("pve1", 300, target_is_running=True)
    assert "Scan could not request pve1/200" in caplog.text


async def test_scan_all_reuses_manager_scan_semaphore(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Three accepted scans use the existing two-slot semaphore and tracked tasks."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    _add_container(coordinator, 300)
    _add_container(coordinator, 301)
    manager = coordinator.package_manager
    transport = GateTransport()
    with patch.object(manager, "_transport", transport):
        await _scan_all(hass)
        tasks = list(manager._tasks.values())
        try:
            await asyncio.wait_for(transport.entered(200).wait(), 1)
            await asyncio.wait_for(transport.entered(300).wait(), 1)
            assert transport.calls == [("pve1", 200), ("pve1", 300)]
            assert manager.record("pve1", 301).status is PackageScanStatus.RUNNING
            transport.release(200)
            await asyncio.wait_for(transport.entered(301).wait(), 1)
        finally:
            for vmid in (200, 300, 301):
                transport.release(vmid)
            await asyncio.gather(*tasks)
    assert transport.max_active == 2
    assert all(
        manager.record("pve1", vmid).status is PackageScanStatus.SUCCESS
        for vmid in (200, 300, 301)
    )


async def test_scan_all_multiple_loaded_entries_and_skips_unconfigured(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Each loaded configured entry uses its own manager; unloaded entries do not."""
    await setup_integration(hass, mock_config_entry)
    other = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data={**deepcopy(dict(mock_config_entry.data)), CONF_HOST: "other-pve"},
    )
    await setup_integration(hass, other)
    unconfigured = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data={
            key: value
            for key, value in other.data.items()
            if key not in (CONF_PACKAGE_NODE, CONF_SSH_HOST_KEY, CONF_SSH_PRIVATE_KEY)
        },
    )
    await setup_integration(hass, unconfigured)
    first_manager = mock_config_entry.runtime_data.package_manager
    other_manager = other.runtime_data.package_manager
    with (
        patch.object(first_manager, "async_start_scan") as first,
        patch.object(other_manager, "async_start_scan") as second,
        patch.object(
            unconfigured.runtime_data.package_manager, "async_start_scan"
        ) as skipped,
    ):
        await _scan_all(hass)
        assert first.call_count == second.call_count == 2
        skipped.assert_not_called()
        await hass.config_entries.async_unload(other.entry_id)
        first.reset_mock()
        second.reset_mock()
        await _scan_all(hass)
        assert first.call_count == 2
        second.assert_not_called()


@pytest.mark.parametrize("unavailable", ["coordinator", "package_node"])
async def test_scan_all_unavailable_entry_is_skipped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    unavailable: str,
) -> None:
    """Missing package node and stale unavailable coordinator cannot supply targets."""
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    if unavailable == "coordinator":
        coordinator.last_update_success = False
    else:
        coordinator.package_node = "missing"
    with patch.object(coordinator.package_manager, "async_start_scan") as start:
        await _scan_all(hass)
    start.assert_not_called()
