"""Tests for package evidence invalidation across LXC runtime observations."""

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.packages.manager import PackageManager
from custom_components.hubinet_ops.packages.models import (
    CleanupEvidence,
    PackageScanRecord,
    PackageScanResult,
    PackageScanStatus,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
    PendingPackage,
    RemovablePackage,
)
from custom_components.hubinet_ops.packages.parser import ParsedAutoremoveSimulation
from homeassistant.components import persistent_notification as pn
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from . import setup_integration
from .test_packages import RESULT, GateTransport

SCAN = "button.ct_nginx_scan_pending_packages"
REVIEW = "button.ct_nginx_review_package_update"
APPROVE = "button.ct_nginx_approve_reviewed_plan"
UPDATE = "button.ct_nginx_update_packages"
AUTOREMOVE = "button.ct_nginx_autoremove_unused_packages"
PENDING = "sensor.ct_nginx_pending_package_updates"
UNUSED = "sensor.ct_nginx_unused_packages"
KEY = ("pve1", 200)
NOW = datetime(2026, 9, 10, tzinfo=UTC)
CANDIDATES = (RemovablePackage("unused", "amd64", "1.0"),)


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    """Press one package button and allow its background work to finish."""
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


def _set_container_status(mock_proxmox_client: MagicMock, status: str) -> None:
    """Set CT200's next current Proxmox status observation."""
    containers = deepcopy(mock_proxmox_client._node_mock.lxc.get.return_value)  # noqa: SLF001
    for container in containers:
        if container["vmid"] == "200":
            container["status"] = status
    mock_proxmox_client._node_mock.lxc.get.return_value = containers  # noqa: SLF001


async def test_stop_restart_discards_all_evidence_and_manual_scan_is_fresh(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Stop invalidates Scan/Review/cleanup UI; restart needs a manual Scan."""
    fresh_result = PackageScanResult(
        os_id=RESULT.os_id,
        os_version=RESULT.os_version,
        packages=(
            PendingPackage("fresh", "amd64", "2.0", "2.1", None, False),
        ),
        reboot_required=False,
        not_upgraded_count=0,
    )
    scan = AsyncMock(side_effect=(RESULT, fresh_result))
    cleanup = AsyncMock(
        return_value=ParsedAutoremoveSimulation(CANDIDATES, not_upgraded_count=0)
    )
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            scan,
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            cleanup,
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN)
        manager = mock_config_entry.runtime_data.package_manager
        old_token = manager.record(*KEY).token
        assert old_token is not None
        assert manager.cleanup_evidence(*KEY) is not None
        await _press(hass, REVIEW)
        await _press(hass, APPROVE)
        assert manager.record(*KEY).reviewed is True

        notifications = pn._async_get_or_create_notifications(hass)  # noqa: SLF001
        review_id = "hubinet_ops_package_review_pve1_200"
        cleanup_id = "hubinet_ops_package_cleanup_candidates_pve1_200"
        assert review_id in notifications
        assert cleanup_id in notifications

        _set_container_status(mock_proxmox_client, "stopped")
        await mock_config_entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

        assert manager.record(*KEY) == PackageScanRecord()
        assert manager.cleanup_evidence(*KEY) is None
        assert manager.viewed_token(*KEY) is None
        assert manager.confirm_review(*KEY, old_token) is False
        assert review_id not in notifications
        assert cleanup_id not in notifications
        assert hass.states.get(PENDING).state == STATE_UNAVAILABLE
        assert hass.states.get(UNUSED).state == STATE_UNAVAILABLE

        original_listener = manager._on_state_change  # noqa: SLF001
        listener = MagicMock(wraps=original_listener)
        manager._on_state_change = listener  # noqa: SLF001
        await mock_config_entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        listener.assert_not_called()
        manager._on_state_change = original_listener  # noqa: SLF001

        _set_container_status(mock_proxmox_client, "running")
        await mock_config_entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(PENDING).state == STATE_UNKNOWN
        assert hass.states.get(UNUSED).state == STATE_UNKNOWN
        for entity_id in (REVIEW, APPROVE, UPDATE, AUTOREMOVE):
            assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
        assert scan.await_count == 1

        await _press(hass, SCAN)
        new_record = manager.record(*KEY)
        assert hass.states.get(PENDING).state == "1"
        assert new_record.status is PackageScanStatus.SUCCESS
        assert new_record.token is not None
        assert new_record.token != old_token
        assert scan.await_count == 2


async def test_non_running_observation_cancels_scan_without_stale_resurrection(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """RUNNING ownership and its task disappear atomically on invalidation."""
    transport = GateTransport()
    manager = PackageManager(
        hass,
        mock_config_entry,
        transport=transport,
        on_state_change=MagicMock(),
        now=lambda: NOW,
    )
    stale_task = manager.async_start_scan(*KEY, target_is_running=True)
    await asyncio.wait_for(transport.entered(KEY[1]).wait(), 1)

    manager.async_invalidate_non_running(set())
    assert manager.record(*KEY) == PackageScanRecord()
    assert KEY in manager._fenced_evidence  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await stale_task
    assert manager.record(*KEY) == PackageScanRecord()

    transport.rearm(KEY[1])
    fresh_task = manager.async_start_scan(*KEY, target_is_running=True)
    assert KEY not in manager._fenced_evidence  # noqa: SLF001
    await asyncio.wait_for(transport.entered(KEY[1]).wait(), 1)
    assert transport.calls == [KEY, KEY]
    transport.release(KEY[1])
    await fresh_task
    assert manager.record(*KEY).status is PackageScanStatus.SUCCESS

    manager.async_invalidate_non_running(set())
    assert KEY in manager._fenced_evidence  # noqa: SLF001
    manager.async_prune(set())
    assert KEY not in manager._fenced_evidence  # noqa: SLF001


async def test_non_running_invalidation_preserves_mutation_attempts_and_history(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Update/Autoremove tasks and terminal attempt facts are never invalidated."""
    manager = PackageManager(
        hass,
        mock_config_entry,
        transport=GateTransport(),
        on_state_change=MagicMock(),
        now=lambda: NOW,
    )
    manager._records[KEY] = PackageScanRecord(  # noqa: SLF001
        status=PackageScanStatus.SUCCESS,
        last_attempt=NOW,
        result=RESULT,
        token="current",
    )
    running_update = PackageUpdateRecord(
        status=PackageUpdateStatus.RUNNING, last_attempt=NOW
    )
    running_cleanup = PackageUpdateRecord(
        status=PackageUpdateStatus.RUNNING, last_attempt=NOW
    )
    manager._update_records[KEY] = running_update  # noqa: SLF001
    manager._cleanup_records[KEY] = running_cleanup  # noqa: SLF001
    update_task = asyncio.create_task(asyncio.Event().wait())
    cleanup_task = asyncio.create_task(asyncio.Event().wait())
    manager._update_tasks[KEY] = update_task  # noqa: SLF001
    manager._cleanup_tasks[KEY] = cleanup_task  # noqa: SLF001

    manager.async_invalidate_non_running(set())
    await asyncio.sleep(0)
    assert not update_task.done()
    assert not cleanup_task.done()
    assert manager.update_record(*KEY) is running_update
    assert manager.cleanup_record(*KEY) is running_cleanup

    terminal_update = PackageUpdateRecord(
        status=PackageUpdateStatus.SUCCESS,
        last_attempt=NOW,
        outcome=PackageUpdateOutcome.SUCCESS,
    )
    terminal_cleanup = PackageUpdateRecord(
        status=PackageUpdateStatus.FAILED,
        last_attempt=NOW,
        outcome=PackageUpdateOutcome.MUTATION_FAILED,
    )
    manager._update_records[KEY] = terminal_update  # noqa: SLF001
    manager._cleanup_records[KEY] = terminal_cleanup  # noqa: SLF001
    manager.async_invalidate_non_running(set())
    assert manager.update_record(*KEY) is terminal_update
    assert manager.cleanup_record(*KEY) is terminal_cleanup

    for task in (update_task, cleanup_task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_cleanup_and_review_dismissal_failures_do_not_block_invalidation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Presentation failures cannot keep stale package evidence actionable."""
    manager = PackageManager(
        hass,
        mock_config_entry,
        transport=GateTransport(),
        on_state_change=MagicMock(),
        now=lambda: NOW,
        on_review_invalidated=MagicMock(side_effect=RuntimeError("review UI")),
        on_cleanup_invalidated=MagicMock(side_effect=RuntimeError("cleanup UI")),
    )
    manager._records[KEY] = PackageScanRecord(  # noqa: SLF001
        status=PackageScanStatus.SUCCESS,
        last_attempt=NOW,
        result=RESULT,
        token="current",
        reviewed=True,
    )
    manager._cleanup_evidence[KEY] = CleanupEvidence(CANDIDATES, NOW)  # noqa: SLF001

    manager.async_invalidate_non_running(set())

    assert manager.record(*KEY) == PackageScanRecord()
    assert manager.cleanup_evidence(*KEY) is None


async def test_offline_node_uses_existing_prune_before_non_running_invalidation(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """An offline node still prunes absent targets without double handling."""
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        AsyncMock(return_value=RESULT),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN)
    manager = mock_config_entry.runtime_data.package_manager
    assert manager.record(*KEY).status is PackageScanStatus.SUCCESS

    nodes = deepcopy(mock_proxmox_client.nodes.get.return_value)
    for node in nodes:
        if node["node"] == KEY[0]:
            node["status"] = "offline"
    mock_proxmox_client.nodes.get.return_value = nodes
    review_dismissed = MagicMock()
    manager._on_review_invalidated = review_dismissed  # noqa: SLF001

    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert manager.record(*KEY) == PackageScanRecord()
    review_dismissed.assert_called_once_with(*KEY)
