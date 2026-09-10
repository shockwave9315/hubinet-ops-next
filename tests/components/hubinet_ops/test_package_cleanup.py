"""Adversarial tests for ephemeral explicit package cleanup orchestration."""

import asyncio
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.packages.manager import PackageManager
from custom_components.hubinet_ops.packages.models import (
    PackageMutationResult,
    PackageScanResult,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
    PendingPackage,
    RemovablePackage,
)
from custom_components.hubinet_ops.packages.parser import (
    ParsedAptSimulation,
    ParsedAutoremoveSimulation,
)
from custom_components.hubinet_ops.packages.snapshots import SnapshotError
from homeassistant.core import HomeAssistant

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
PENDING = (
    PendingPackage("openssl", "amd64", "1.0", "1.1", "Debian-Security", True),
)
SCAN_RESULT = PackageScanResult("debian", "12", PENDING, None, 0)


def _candidates(count: int, *, version: str = "1.0") -> tuple[RemovablePackage, ...]:
    return tuple(
        RemovablePackage(f"unused-{index}", "amd64", version)
        for index in range(count)
    )


class CleanupTransport:
    """Typed transport double with sequenced cleanup observations."""

    configured = True
    observed_helper_version = 4

    def __init__(self, plans: list[object]) -> None:
        self.plans = deque(plans)
        self.plan_calls = 0
        self.autoremove_calls = 0
        self.update_calls = 0
        self.autoremove_result: PackageMutationResult | PackageUpdateError = (
            PackageMutationResult(
                before={(item.name, item.architecture): item.installed_version for item in _candidates(7)},
                after={},
            )
        )
        self.ping_results: deque[bool | PackageUpdateError] = deque([True])

    async def async_prepare(self, hass: HomeAssistant) -> None:
        """Perform no remote work during immediate preparation."""

    async def async_probe(self) -> object:
        """Return the current helper version for compatibility UX."""
        return SimpleNamespace(helper_version=self.observed_helper_version)

    async def async_scan(self, expected_node: str, vmid: int) -> PackageScanResult:
        return SCAN_RESULT

    async def async_plan(
        self, expected_node: str, vmid: int
    ) -> ParsedAptSimulation:
        return ParsedAptSimulation(PENDING, 0)

    async def async_update(
        self, expected_node: str, vmid: int
    ) -> PackageMutationResult:
        self.update_calls += 1
        return PackageMutationResult(
            before={("openssl", "amd64"): "1.0"},
            after={("openssl", "amd64"): "1.1"},
        )

    async def async_plan_autoremove(
        self, expected_node: str, vmid: int
    ) -> ParsedAutoremoveSimulation:
        self.plan_calls += 1
        result = self.plans.popleft()
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, ParsedAutoremoveSimulation)
        return result

    async def async_autoremove(
        self, expected_node: str, vmid: int
    ) -> PackageMutationResult:
        self.autoremove_calls += 1
        if isinstance(self.autoremove_result, PackageUpdateError):
            raise self.autoremove_result
        return self.autoremove_result

    async def async_ping(self, expected_node: str, vmid: int) -> bool:
        result = self.ping_results.popleft()
        if isinstance(result, PackageUpdateError):
            raise result
        return result


def _parsed(candidates: tuple[RemovablePackage, ...]) -> ParsedAutoremoveSimulation:
    return ParsedAutoremoveSimulation(candidates, 0)


def _manager(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    transport: CleanupTransport,
    *,
    presentation: MagicMock | None = None,
    invalidated: MagicMock | None = None,
    cleanup_complete: MagicMock | None = None,
) -> tuple[PackageManager, MagicMock]:
    proxmox = MagicMock()
    proxmox.nodes.return_value.lxc.return_value.status.current.get.return_value = {
        "status": "running"
    }
    manager = PackageManager(
        hass,
        entry,
        transport=transport,
        on_state_change=MagicMock(),
        now=lambda: NOW,
        proxmox_getter=lambda: proxmox,
        sleep=AsyncMock(),
        on_cleanup_observation=presentation or MagicMock(),
        on_cleanup_invalidated=invalidated or MagicMock(),
        on_cleanup_complete=cleanup_complete or MagicMock(),
    )
    return manager, proxmox


async def _scan(manager: PackageManager) -> None:
    await manager.async_start_scan("pve1", 200, target_is_running=True)


async def _review(manager: PackageManager) -> None:
    record = manager.record("pve1", 200)
    assert record.token is not None
    assert manager.confirm_review("pve1", 200, record.token)


def _snapshot_patches():
    return (
        patch(
            "custom_components.hubinet_ops.packages.manager.async_list_snapshots",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_delete_snapshot",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.generate_snapshot_name",
            return_value="hubinet-preupd-20260910120000-ab12cd",
        ),
    )


async def test_scan_cleanup_observation_is_independent_and_presentation_gated(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    candidates = _candidates(7)
    presentation = MagicMock()
    transport = CleanupTransport([_parsed(candidates)])
    manager, _ = _manager(
        hass, mock_config_entry, transport, presentation=presentation
    )
    await _scan(manager)
    assert manager.record("pve1", 200).status is PackageScanStatus.SUCCESS
    assert manager.cleanup_evidence("pve1", 200).candidates == candidates
    presentation.assert_called_once_with("pve1", 200, candidates, "scan")

    failing_presentation = MagicMock(side_effect=RuntimeError("renderer failed"))
    transport = CleanupTransport([_parsed(candidates)])
    manager, _ = _manager(
        hass, mock_config_entry, transport, presentation=failing_presentation
    )
    await _scan(manager)
    assert manager.record("pve1", 200).status is PackageScanStatus.SUCCESS
    assert manager.cleanup_evidence("pve1", 200) is None
    with pytest.raises(PackageUpdateError):
        manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )


async def test_slow_startup_probe_never_blocks_prepare(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The one-shot remote probe is lifecycle tracked but never awaited by setup."""
    transport = CleanupTransport([_parsed(())])
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_probe() -> object:
        entered.set()
        await release.wait()
        return SimpleNamespace(helper_version=4)

    transport.async_probe = blocked_probe  # type: ignore[method-assign]
    observed = MagicMock()
    manager = PackageManager(
        hass,
        mock_config_entry,
        transport=transport,
        on_state_change=MagicMock(),
        on_helper_version=observed,
    )
    await asyncio.wait_for(manager.async_prepare(), timeout=0.1)
    await entered.wait()
    assert manager.helper_version is None
    release.set()
    await hass.async_block_till_done()
    assert manager.helper_version == 4
    observed.assert_called_once_with(4)


async def test_probe_failure_is_best_effort_and_unknown_is_not_old(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Missing or failed version evidence remains unknown and setup-safe."""
    transport = CleanupTransport([_parsed(())])

    async def failed_probe() -> object:
        raise RuntimeError("offline")

    transport.async_probe = failed_probe  # type: ignore[method-assign]
    transport.observed_helper_version = None
    observed = MagicMock()
    manager = PackageManager(
        hass,
        mock_config_entry,
        transport=transport,
        on_state_change=MagicMock(),
        on_helper_version=observed,
    )
    await manager.async_prepare()
    await hass.async_block_till_done()
    assert manager.helper_version is None
    observed.assert_not_called()


async def test_failed_new_operation_still_updates_manager_helper_version(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A v3 operation mismatch updates the scalar while cleanup stays unknown."""
    transport = CleanupTransport(
        [PackageUpdateError(PackageUpdateOutcome.HELPER_OUTDATED, "outdated")]
    )
    transport.observed_helper_version = 3
    observed = MagicMock()
    manager = PackageManager(
        hass,
        mock_config_entry,
        transport=transport,
        on_state_change=MagicMock(),
        on_helper_version=observed,
    )
    await _scan(manager)
    assert manager.record("pve1", 200).status is PackageScanStatus.SUCCESS
    assert manager.cleanup_evidence("pve1", 200) is None
    assert manager.helper_version == 3
    observed.assert_called_once_with(3)


async def test_scan_cleanup_failure_is_unknown_but_scan_stays_success(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    transport = CleanupTransport(
        [PackageUpdateError(PackageUpdateOutcome.PLAN_FAILED, "failed")]
    )
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    assert manager.record("pve1", 200).status is PackageScanStatus.SUCCESS
    assert manager.cleanup_evidence("pve1", 200) is None


async def test_zero_cleanup_is_known_but_never_actionable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    manager, _ = _manager(
        hass, mock_config_entry, CleanupTransport([_parsed(())])
    )
    await _scan(manager)
    assert manager.cleanup_evidence("pve1", 200).candidates == ()
    with pytest.raises(PackageUpdateError) as caught:
        manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    assert caught.value.outcome is PackageUpdateOutcome.PLAN_FAILED


async def test_update_invalidates_old_cleanup_then_observes_without_changing_success(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    old = _candidates(7)
    transport = CleanupTransport(
        [_parsed(old), PackageUpdateError(PackageUpdateOutcome.PLAN_FAILED, "failed")]
    )
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    await _review(manager)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        task = manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        assert manager.cleanup_evidence("pve1", 200) is None
        await task
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert manager.cleanup_evidence("pve1", 200) is None


@pytest.mark.parametrize(
    "fresh",
    [_candidates(6), _candidates(8), _candidates(7, version="2.0")],
    ids=["removed", "added", "version_changed"],
)
async def test_cleanup_plan_change_stops_before_snapshot_and_mutation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fresh: tuple[RemovablePackage, ...],
) -> None:
    shown = _candidates(7)
    transport = CleanupTransport([_parsed(shown), _parsed(fresh)])
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    patches = _snapshot_patches()
    with patches[0] as listing, patches[1] as create, patches[2], patches[3]:
        await manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.cleanup_record("pve1", 200)
    assert record.outcome is PackageUpdateOutcome.PLAN_CHANGED
    assert transport.autoremove_calls == 0
    listing.assert_not_awaited()
    create.assert_not_awaited()
    assert manager.cleanup_evidence("pve1", 200) is None


async def test_exact_cleanup_plan_reuses_safety_path_and_observes_zero(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    shown = _candidates(7)
    complete = MagicMock()
    transport = CleanupTransport([_parsed(shown), _parsed(shown), _parsed(())])
    manager, _ = _manager(
        hass, mock_config_entry, transport, cleanup_complete=complete
    )
    await _scan(manager)
    patches = _snapshot_patches()
    with patches[0], patches[1] as create, patches[2] as delete, patches[3]:
        await manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.cleanup_record("pve1", 200)
    assert record.status is PackageUpdateStatus.SUCCESS
    assert record.changed_package_count == 7
    assert record.liveness is True
    assert manager.cleanup_evidence("pve1", 200).candidates == ()
    create.assert_awaited_once()
    delete.assert_awaited_once()
    complete.assert_called_once_with("pve1", 200, record)


async def test_snapshot_uncertainty_never_allows_autoremove(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    shown = _candidates(7)
    transport = CleanupTransport([_parsed(shown), _parsed(shown)])
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    patches = _snapshot_patches()
    with (
        patches[0],
        patch(
            "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
            new=AsyncMock(side_effect=SnapshotError("uncertain", True)),
        ),
        patches[2] as delete,
        patches[3],
    ):
        await manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.cleanup_record("pve1", 200)
    assert record.outcome is PackageUpdateOutcome.SNAPSHOT_FAILED
    assert record.snapshot_uncertain is True
    assert transport.autoremove_calls == 0
    delete.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    [
        PackageUpdateError(PackageUpdateOutcome.MUTATION_FAILED, "failed"),
        PackageUpdateError(PackageUpdateOutcome.MUTATION_TIMED_OUT, "timed out"),
    ],
)
async def test_cleanup_mutation_failure_retains_confirmed_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    failure: PackageUpdateError,
) -> None:
    shown = _candidates(7)
    transport = CleanupTransport([_parsed(shown), _parsed(shown)])
    transport.autoremove_result = failure
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2] as delete, patches[3]:
        await manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.cleanup_record("pve1", 200)
    assert record.outcome is failure.outcome
    assert record.snapshot_retained is True
    assert record.snapshot_name == "hubinet-preupd-20260910120000-ab12cd"
    delete.assert_not_awaited()


async def test_snapshot_delete_failure_keeps_confirmed_mutation_success(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    shown = _candidates(7)
    transport = CleanupTransport([_parsed(shown), _parsed(shown), _parsed(())])
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    patches = _snapshot_patches()
    with (
        patches[0],
        patches[1],
        patch(
            "custom_components.hubinet_ops.packages.manager.async_delete_snapshot",
            new=AsyncMock(side_effect=SnapshotError("delete", True)),
        ),
        patches[3],
    ):
        await manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.cleanup_record("pve1", 200)
    assert record.status is PackageUpdateStatus.SUCCESS
    assert record.snapshot_cleanup_failed is True
    assert record.snapshot_retained is True
    assert record.snapshot_name == "hubinet-preupd-20260910120000-ab12cd"


async def test_post_cleanup_observation_failure_preserves_mutation_success(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    shown = _candidates(7)
    transport = CleanupTransport(
        [
            _parsed(shown),
            _parsed(shown),
            PackageUpdateError(PackageUpdateOutcome.PLAN_FAILED, "failed"),
        ]
    )
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    assert manager.cleanup_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert manager.cleanup_evidence("pve1", 200) is None


async def test_cleanup_press_invalidates_synchronously_and_double_press_fails(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    shown = _candidates(7)
    transport = CleanupTransport([_parsed(shown), _parsed(shown), _parsed(())])
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    original_plan = transport.async_plan_autoremove
    plan_entered = asyncio.Event()
    plan_release = asyncio.Event()

    async def gated_plan(node: str, vmid: int) -> ParsedAutoremoveSimulation:
        plan_entered.set()
        await plan_release.wait()
        return await original_plan(node, vmid)

    transport.async_plan_autoremove = gated_plan  # type: ignore[method-assign]
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        task = manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        await plan_entered.wait()
        assert manager.cleanup_evidence("pve1", 200) is None
        assert manager.cleanup_record("pve1", 200).status is PackageUpdateStatus.RUNNING
        with pytest.raises(PackageUpdateError) as caught:
            manager.async_start_autoremove(
                "pve1", 200, target_is_running=True, snapshot_permission=True
            )
        assert caught.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY
        plan_release.set()
        await task


async def test_prune_removes_cleanup_state_and_stale_task_cannot_resurrect(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    shown = _candidates(7)
    transport = CleanupTransport([_parsed(shown), _parsed(shown)])
    entered = asyncio.Event()

    async def blocked_autoremove(_node: str, _vmid: int) -> PackageMutationResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    transport.async_autoremove = blocked_autoremove  # type: ignore[method-assign]
    manager, _ = _manager(hass, mock_config_entry, transport)
    await _scan(manager)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        task = manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        await entered.wait()
        manager.async_prune(set())
        with pytest.raises(asyncio.CancelledError):
            await task
    assert manager.cleanup_evidence("pve1", 200) is None
    assert manager.cleanup_record("pve1", 200) == PackageUpdateRecord()
