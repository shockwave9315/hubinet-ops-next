"""Tests for minimal fail-closed PackageManager Restore exclusion."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.packages.manager import PackageManager
from custom_components.hubinet_ops.packages.models import (
    CleanupEvidence,
    PackageScanError,
    PackageScanRecord,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
    RemovablePackage,
)
from homeassistant.core import HomeAssistant

from .test_packages import RESULT, GateTransport

KEY = ("pve1", 200)
NOW = datetime(2026, 9, 11, tzinfo=UTC)


def _manager(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    **callbacks,
) -> PackageManager:
    return PackageManager(
        hass,
        entry,
        transport=GateTransport(),
        on_state_change=MagicMock(),
        now=lambda: NOW,
        proxmox_getter=MagicMock(),
        **callbacks,
    )


async def test_scan_claim_first_blocks_restore(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A synchronous RUNNING scan claim wins before Restore reservation."""
    manager = _manager(hass, mock_config_entry)
    task = manager.async_start_scan(*KEY, target_is_running=True)
    assert manager.record(*KEY).status is PackageScanStatus.RUNNING
    with pytest.raises(PackageUpdateError) as error:
        manager.begin_restore(*KEY)
    assert error.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_restore_claim_first_blocks_every_package_start(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The synchronous reservation wins before Scan, Update, and Autoremove."""
    manager = _manager(hass, mock_config_entry)
    manager.begin_restore(*KEY)

    with pytest.raises(PackageScanError):
        manager.async_start_scan(*KEY, target_is_running=True)
    with pytest.raises(PackageUpdateError) as update_error:
        manager.async_start_update(
            *KEY, target_is_running=True, snapshot_permission=True
        )
    with pytest.raises(PackageUpdateError) as cleanup_error:
        manager.async_start_autoremove(
            *KEY, target_is_running=True, snapshot_permission=True
        )
    assert update_error.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY
    assert cleanup_error.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY


def test_second_restore_same_vmid_is_busy(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """One VMID has one reservation without reference counting."""
    manager = _manager(hass, mock_config_entry)
    manager.begin_restore(*KEY)
    with pytest.raises(PackageUpdateError) as error:
        manager.begin_restore("other-node", KEY[1])
    assert error.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY


@pytest.mark.parametrize("operation", ["scan", "update", "cleanup"])
def test_each_active_package_operation_blocks_restore(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    operation: str,
) -> None:
    """Restore observes every existing synchronous RUNNING claim."""
    manager = _manager(hass, mock_config_entry)
    if operation == "scan":
        manager._records[KEY] = PackageScanRecord(  # noqa: SLF001
            status=PackageScanStatus.RUNNING, last_attempt=NOW
        )
    else:
        records = (
            manager._update_records  # noqa: SLF001
            if operation == "update"
            else manager._cleanup_records  # noqa: SLF001
        )
        records[KEY] = PackageUpdateRecord(
            status=PackageUpdateStatus.RUNNING, last_attempt=NOW
        )
    with pytest.raises(PackageUpdateError) as error:
        manager.begin_restore(*KEY)
    assert error.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY


def test_restore_invalidation_preserves_terminal_mutation_history(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Invalidate current truth and UI while preserving terminal facts."""
    review_invalidated = MagicMock()
    cleanup_invalidated = MagicMock()
    manager = _manager(
        hass,
        mock_config_entry,
        on_review_invalidated=review_invalidated,
        on_cleanup_invalidated=cleanup_invalidated,
    )
    manager._records[KEY] = PackageScanRecord(  # noqa: SLF001
        status=PackageScanStatus.SUCCESS,
        last_attempt=NOW,
        result=RESULT,
        token="token",
        reviewed=True,
    )
    manager._viewed_tokens[KEY] = "token"  # noqa: SLF001
    manager._cleanup_evidence[KEY] = CleanupEvidence(  # noqa: SLF001
        candidates=(RemovablePackage("old", "amd64", "1"),), observed_at=NOW
    )
    update = PackageUpdateRecord(
        status=PackageUpdateStatus.SUCCESS,
        last_attempt=NOW,
        outcome=PackageUpdateOutcome.SUCCESS,
    )
    cleanup = PackageUpdateRecord(
        status=PackageUpdateStatus.FAILED,
        last_attempt=NOW,
        outcome=PackageUpdateOutcome.MUTATION_FAILED,
    )
    manager._update_records[KEY] = update  # noqa: SLF001
    manager._cleanup_records[KEY] = cleanup  # noqa: SLF001

    manager.begin_restore(*KEY)
    manager.invalidate_restore_target(*KEY)

    assert manager.record(*KEY) == PackageScanRecord()
    assert manager.viewed_token(*KEY) is None
    assert manager.cleanup_evidence(*KEY) is None
    assert manager.update_record(*KEY) is update
    assert manager.cleanup_record(*KEY) is cleanup
    assert KEY in manager._fenced_evidence  # noqa: SLF001
    review_invalidated.assert_called_once_with(*KEY)
    cleanup_invalidated.assert_called_once_with(*KEY)


async def test_manual_scan_after_release_establishes_new_truth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A released known outcome permits manual Scan to clear the evidence fence."""
    manager = _manager(hass, mock_config_entry)
    manager.begin_restore(*KEY)
    manager.invalidate_restore_target(*KEY)
    manager.end_restore(*KEY)
    task = manager.async_start_scan(*KEY, target_is_running=True)
    assert KEY not in manager._fenced_evidence  # noqa: SLF001
    manager._transport.release(KEY[1])  # noqa: SLF001
    await task
    assert manager.record(*KEY).status is PackageScanStatus.SUCCESS


def test_pruning_does_not_clear_uncertain_reservation(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Removed or reused VMIDs stay fail-closed for this manager lifetime."""
    manager = _manager(hass, mock_config_entry)
    manager.begin_restore(*KEY)
    manager.invalidate_restore_target(*KEY)
    manager.async_prune(set())
    assert manager.restore_reserved(*KEY)
    with pytest.raises(PackageScanError):
        manager.async_start_scan(*KEY, target_is_running=True)
