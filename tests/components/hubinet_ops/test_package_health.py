"""Tests for LXC Health: the pure classifier, manager, and entity surface."""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.packages.manager import PackageManager
from custom_components.hubinet_ops.packages.models import (
    HealthCheckStatus,
    HealthDpkgState,
    HealthReason,
    HealthSource,
    HealthState,
    PackageHealthEvidence,
    PackageHealthOutcome,
    PackageHealthRecord,
    PackageScanError,
    PackageScanFailure,
    PackageScanRecord,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
    classify_health,
)
from custom_components.hubinet_ops.packages.transport import PackageHealthError
from homeassistant.components import persistent_notification as pn
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from . import setup_integration
from .test_package_cleanup import (
    CleanupTransport,
    _candidates,
    _manager as _cleanup_manager,
    _parsed,
    _scan as _cleanup_scan,
    _snapshot_patches as _cleanup_snapshot_patches,
)
from .test_packages import (
    ATTEMPTED_AT,
    RESULT,
    UpdateTransport,
    _review_target,
    _snapshot_patches as _update_snapshot_patches,
    _update_manager,
)

HEALTH_BUTTON = "button.ct_nginx_health_check"
HEALTH_SENSOR = "sensor.ct_nginx_health"
SCAN_BUTTON = "button.ct_nginx_scan_pending_packages"
REVIEW_BUTTON = "button.ct_nginx_review_package_update"
APPROVE_BUTTON = "button.ct_nginx_approve_reviewed_plan"
UPDATE_BUTTON = "button.ct_nginx_update_packages"
AUTOREMOVE_BUTTON = "button.ct_nginx_autoremove_unused_packages"

# ---------------------------------------------------------------------------
# CLASSIFIER
# ---------------------------------------------------------------------------


def _evidence(**overrides: object) -> PackageHealthEvidence:
    base: dict[str, object] = {
        "guest_exec": True,
        "guest_exec_unavailable": False,
        "dpkg": HealthDpkgState.OK,
        "unfinished_package_count": None,
        "reboot_required": None,
    }
    base.update(overrides)
    return PackageHealthEvidence(**base)


def test_classify_healthy_requires_clean_dpkg_and_no_reboot_marker() -> None:
    """A clean dpkg state with no reboot marker is HEALTHY."""
    outcome = classify_health(_evidence())
    assert outcome.state is HealthState.HEALTHY
    assert outcome.reason is None


def test_classify_degraded_requires_clean_dpkg_and_positive_reboot_marker() -> None:
    """A clean dpkg state plus a positive reboot marker is DEGRADED."""
    outcome = classify_health(_evidence(reboot_required=True))
    assert outcome.state is HealthState.DEGRADED
    assert outcome.reason is None


def test_classify_failed_guest_exec_requires_guest_still_running() -> None:
    """A positive exec failure while still running is FAILED."""
    outcome = classify_health(
        _evidence(guest_exec=False, guest_exec_unavailable=False, dpkg=None)
    )
    assert outcome.state is HealthState.FAILED
    assert outcome.reason is HealthReason.GUEST_EXEC_FAILED


def test_classify_unknown_guest_exec_when_guest_no_longer_running() -> None:
    """An exec failure explained by the guest stopping is UNKNOWN, not FAILED."""
    outcome = classify_health(
        _evidence(guest_exec=False, guest_exec_unavailable=True, dpkg=None)
    )
    assert outcome.state is None
    assert outcome.reason is HealthReason.GUEST_UNAVAILABLE


def test_classify_failed_dpkg_interrupted() -> None:
    """A persistently half-installed identity set is FAILED."""
    outcome = classify_health(
        _evidence(dpkg=HealthDpkgState.INTERRUPTED, unfinished_package_count=1)
    )
    assert outcome.state is HealthState.FAILED
    assert outcome.reason is HealthReason.DPKG_INTERRUPTED


@pytest.mark.parametrize(
    ("dpkg", "reason"),
    [
        (HealthDpkgState.PENDING, HealthReason.DPKG_PENDING),
        (HealthDpkgState.BUSY, HealthReason.PACKAGE_MANAGER_BUSY),
        (HealthDpkgState.LOCK_UNKNOWN, HealthReason.DPKG_LOCK_UNKNOWN),
        (HealthDpkgState.CHANGED, HealthReason.GUEST_CHANGED),
    ],
)
def test_classify_every_dpkg_unknown_reason(
    dpkg: HealthDpkgState, reason: HealthReason
) -> None:
    """Every unresolved dpkg classification is UNKNOWN, never FAILED."""
    outcome = classify_health(_evidence(dpkg=dpkg))
    assert outcome.state is None
    assert outcome.reason is reason


def test_classify_missing_dpkg_evidence_is_unknown_malformed() -> None:
    """A missing dpkg classification while guest_exec is true fails closed."""
    outcome = classify_health(_evidence(dpkg=None))
    assert outcome.state is None
    assert outcome.reason is HealthReason.MALFORMED_EVIDENCE


def test_classify_failed_takes_precedence_over_degraded() -> None:
    """A positive reboot marker never promotes an interrupted dpkg to DEGRADED."""
    outcome = classify_health(
        _evidence(
            dpkg=HealthDpkgState.INTERRUPTED,
            unfinished_package_count=1,
            reboot_required=True,
        )
    )
    assert outcome.state is HealthState.FAILED
    assert outcome.reason is HealthReason.DPKG_INTERRUPTED


def test_classify_unknown_dpkg_prevents_healthy_or_degraded() -> None:
    """A busy/uncertain dpkg lock stays UNKNOWN regardless of the reboot marker."""
    outcome = classify_health(
        _evidence(dpkg=HealthDpkgState.BUSY, reboot_required=True)
    )
    assert outcome.state is None
    assert outcome.reason is HealthReason.PACKAGE_MANAGER_BUSY


# ---------------------------------------------------------------------------
# MANAGER
# ---------------------------------------------------------------------------


def _default_health_outcome() -> PackageHealthOutcome:
    return PackageHealthOutcome(
        HealthState.HEALTHY,
        None,
        PackageHealthEvidence(
            guest_exec=True,
            guest_exec_unavailable=False,
            dpkg=HealthDpkgState.OK,
            unfinished_package_count=None,
            reboot_required=None,
        ),
    )


class HealthTransport:
    """Minimal gated transport exposing only a controllable ``async_check_health``."""

    configured = True

    def __init__(self) -> None:
        """Initialize empty per-key gating and outcomes."""
        self._entered: dict[tuple[str, int], asyncio.Event] = {}
        self._release: dict[tuple[str, int], asyncio.Event] = {}
        self.outcomes: dict[tuple[str, int], PackageHealthOutcome | Exception] = {}
        self.calls: list[tuple[str, int]] = []

    def entered(self, key: tuple[str, int]) -> asyncio.Event:
        """Return the event set once a key's check reaches the transport."""
        return self._entered.setdefault(key, asyncio.Event())

    def release(self, key: tuple[str, int]) -> None:
        """Allow one key's in-flight check to resolve."""
        self._release.setdefault(key, asyncio.Event()).set()

    async def async_check_health(
        self, expected_node: str, vmid: int
    ) -> PackageHealthOutcome:
        """Wait for release before returning the configured outcome."""
        key = (expected_node, vmid)
        self.calls.append(key)
        self.entered(key).set()
        await self._release.setdefault(key, asyncio.Event()).wait()
        outcome = self.outcomes.get(key, _default_health_outcome())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _health_manager(
    hass: HomeAssistant, entry: MockConfigEntry, transport: HealthTransport
) -> PackageManager:
    return PackageManager(
        hass,
        entry,
        transport=transport,
        on_state_change=MagicMock(),
        now=lambda: ATTEMPTED_AT,
        proxmox_getter=lambda: MagicMock(),
    )


async def test_manual_health_claims_running_then_publishes_classified_outcome(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Manual Health returns immediately RUNNING, then a completed classification."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)
    task = manager.async_start_health("pve1", 200, target_is_running=True)
    await transport.entered(key).wait()
    record = manager.health_record(*key)
    assert record.check_status is HealthCheckStatus.RUNNING
    assert record.source is HealthSource.MANUAL
    assert record.state is None

    transport.release(key)
    await task
    record = manager.health_record(*key)
    assert record.check_status is HealthCheckStatus.COMPLETED
    assert record.state is HealthState.HEALTHY
    assert record.checked_at == ATTEMPTED_AT
    assert record.guest_exec is True
    assert record.dpkg is HealthDpkgState.OK


async def test_manual_health_task_creation_failure_rolls_back_and_reraises(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A manual Health task-creation failure rolls back the claim and re-raises.

    The fake deliberately does NOT close the coroutine itself: the
    implementation must own and close it explicitly (F4), never leaving an
    un-awaited coroutine behind, and the caller (the Health button) must
    still see an error rather than a silently stuck RUNNING claim.
    """
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    captured: dict[str, Any] = {}

    def failing_create(hass_arg, coro, name, *args, **kwargs):
        captured["coro"] = coro
        raise RuntimeError("no background task slots")

    with patch.object(
        mock_config_entry,
        "async_create_background_task",
        side_effect=failing_create,
    ):
        with pytest.raises(PackageScanError) as caught:
            manager.async_start_health("pve1", 200, target_is_running=True)

    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED
    assert manager.health_record("pve1", 200) == PackageHealthRecord()
    assert ("pve1", 200) not in manager._health_tasks  # noqa: SLF001
    assert captured["coro"].cr_frame is None


async def test_eagerly_completed_manual_health_leaves_no_stale_task_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """F5: a transport that resolves without ever truly suspending leaves
    no stale ``_health_tasks`` entry once the manual check has completed.
    """

    class ImmediateHealthTransport:
        configured = True

        async def async_check_health(
            self, expected_node: str, vmid: int
        ) -> PackageHealthOutcome:
            return _default_health_outcome()

    manager = _health_manager(hass, mock_config_entry, ImmediateHealthTransport())
    task = manager.async_start_health("pve1", 200, target_is_running=True)
    await task

    assert ("pve1", 200) not in manager._health_tasks  # noqa: SLF001
    record = manager.health_record("pve1", 200)
    assert record.check_status is HealthCheckStatus.COMPLETED
    assert record.state is HealthState.HEALTHY


async def test_second_health_start_for_same_vmid_is_rejected_while_running(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A second manual Health for the same busy VMID is rejected."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)
    manager.async_start_health("pve1", 200, target_is_running=True)
    await transport.entered(key).wait()

    with pytest.raises(PackageScanError) as caught:
        manager.async_start_health("pve1", 200, target_is_running=True)
    assert caught.value.failure is PackageScanFailure.PACKAGE_MANAGER_BUSY
    transport.release(key)


async def test_scan_update_autoremove_rejected_while_health_running(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Health RUNNING joins the existing same-VMID busy model."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)
    manager.async_start_health("pve1", 200, target_is_running=True)
    await transport.entered(key).wait()

    with pytest.raises(PackageScanError) as scan_err:
        manager.async_start_scan("pve1", 200, target_is_running=True)
    assert scan_err.value.failure is PackageScanFailure.PACKAGE_MANAGER_BUSY

    with pytest.raises(PackageUpdateError) as update_err:
        manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    assert update_err.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY

    with pytest.raises(PackageUpdateError) as autoremove_err:
        manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    assert autoremove_err.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY

    transport.release(key)


async def test_manual_health_rejected_while_scan_update_or_autoremove_running(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A running Scan/Update/Autoremove for this VMID rejects manual Health."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)

    manager._records[key] = PackageScanRecord(status=PackageScanStatus.RUNNING)  # noqa: SLF001
    with pytest.raises(PackageScanError) as scan_busy:
        manager.async_start_health("pve1", 200, target_is_running=True)
    assert scan_busy.value.failure is PackageScanFailure.PACKAGE_MANAGER_BUSY
    manager._records.pop(key)  # noqa: SLF001

    manager._update_records[key] = PackageUpdateRecord(  # noqa: SLF001
        status=PackageUpdateStatus.RUNNING
    )
    with pytest.raises(PackageScanError):
        manager.async_start_health("pve1", 200, target_is_running=True)
    manager._update_records.pop(key)  # noqa: SLF001

    manager._cleanup_records[key] = PackageUpdateRecord(  # noqa: SLF001
        status=PackageUpdateStatus.RUNNING
    )
    with pytest.raises(PackageScanError):
        manager.async_start_health("pve1", 200, target_is_running=True)


async def test_health_across_different_vmids_is_independent(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Health RUNNING for one VMID never blocks a different VMID."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    first, second = ("pve1", 200), ("pve1", 201)
    manager.async_start_health(*first, target_is_running=True)
    await transport.entered(first).wait()

    task = manager.async_start_health(*second, target_is_running=True)
    await transport.entered(second).wait()
    assert manager.health_record(*second).check_status is HealthCheckStatus.RUNNING

    transport.release(first)
    transport.release(second)
    await task


async def test_restore_can_start_while_health_runs_and_invalidates_it(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """begin_restore is unaffected by Health; invalidation clears Health after."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)
    task = manager.async_start_health("pve1", 200, target_is_running=True)
    await transport.entered(key).wait()

    manager.begin_restore("pve1", 200)
    assert manager.restore_reserved("pve1", 200) is True
    manager.invalidate_restore_target("pve1", 200)
    assert manager.health_record(*key) == PackageHealthRecord()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_non_running_observation_invalidates_health(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A guest observed not running discards current Health evidence."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)
    task = manager.async_start_health("pve1", 200, target_is_running=True)
    await transport.entered(key).wait()

    manager.async_invalidate_non_running(running_targets=frozenset())
    assert manager.health_record(*key) == PackageHealthRecord()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_prune_invalidates_a_completed_health_record(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A pruned target's completed Health evidence is discarded."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)
    task = manager.async_start_health("pve1", 200, target_is_running=True)
    await transport.entered(key).wait()
    transport.release(key)
    await task
    assert manager.health_record(*key).check_status is HealthCheckStatus.COMPLETED

    manager.async_prune(current_targets=frozenset())
    assert manager.health_record(*key) == PackageHealthRecord()


async def test_identity_guarded_late_health_result_is_discarded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A completion for a since-replaced attempt never overwrites the new one."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)
    task = manager.async_start_health("pve1", 200, target_is_running=True)
    await transport.entered(key).wait()

    replacement = PackageHealthRecord(
        check_status=HealthCheckStatus.RUNNING, source=HealthSource.MANUAL
    )
    manager._health_records[key] = replacement  # noqa: SLF001
    transport.release(key)
    await task
    assert manager.health_record(*key) is replacement


async def test_cancelled_health_task_publishes_no_false_result(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A directly cancelled attempt (unload-style) fabricates no terminal result."""
    transport = HealthTransport()
    manager = _health_manager(hass, mock_config_entry, transport)
    key = ("pve1", 200)
    task = manager.async_start_health("pve1", 200, target_is_running=True)
    await transport.entered(key).wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Nothing rewrote the record this attempt claimed into a false verdict.
    assert manager.health_record(*key).check_status is HealthCheckStatus.RUNNING
    assert manager.health_record(*key).state is None


async def test_update_acceptance_invalidates_old_health(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Accepting Update clears any current Health record for that VMID."""
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    manager._health_records[("pve1", 200)] = PackageHealthRecord(  # noqa: SLF001
        check_status=HealthCheckStatus.COMPLETED,
        state=HealthState.HEALTHY,
        source=HealthSource.MANUAL,
    )
    # Hold the execution-time plan gate so acceptance-time invalidation can
    # be observed before the background attempt produces fresh evidence.
    transport.plan_release.clear()

    patches = _update_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        task = manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        assert manager.health_record("pve1", 200) == PackageHealthRecord()
        transport.plan_release.set()
        await task


async def test_autoremove_acceptance_invalidates_old_health(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Accepting Autoremove clears any current Health record for that VMID."""
    candidates = _candidates(1)
    transport = CleanupTransport([_parsed(candidates), _parsed(candidates)])
    manager, _proxmox = _cleanup_manager(hass, mock_config_entry, transport)
    await _cleanup_scan(manager)
    manager._health_records[("pve1", 200)] = PackageHealthRecord(  # noqa: SLF001
        check_status=HealthCheckStatus.COMPLETED,
        state=HealthState.HEALTHY,
        source=HealthSource.MANUAL,
    )

    release = asyncio.Event()
    real_plan_autoremove = transport.async_plan_autoremove

    async def gated_plan_autoremove(node: str, vmid: int):
        await release.wait()
        return await real_plan_autoremove(node, vmid)

    transport.async_plan_autoremove = gated_plan_autoremove  # type: ignore[method-assign]

    patches = _cleanup_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        task = manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        assert manager.health_record("pve1", 200) == PackageHealthRecord()
        release.set()
        await task


# ---------------------------------------------------------------------------
# F1: RE-ENTRANCY-SAFE POST-OP HAND-OFF
# ---------------------------------------------------------------------------


async def test_reentrant_operations_during_update_success_publish_are_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Health RUNNING is claimed before the SUCCESS publish, not merely after it.

    A synchronous ``on_state_change`` listener -- standing in for an eager
    Home Assistant automation reacting to the just-published SUCCESS state
    -- attempts a re-entrant Scan, manual Health, and Autoremove for the
    same VMID. All three must already see Health RUNNING and be rejected,
    proving the claim happened strictly before publication, not merely
    without an ``await`` after it.
    """
    transport = UpdateTransport()
    manager_holder: list[PackageManager] = []
    attempts: dict[str, Exception | None] = {}
    fired = {"value": False}

    def on_state_change() -> None:
        if not manager_holder or fired["value"]:
            return
        manager = manager_holder[0]
        if manager.update_record("pve1", 200).status is not (
            PackageUpdateStatus.SUCCESS
        ):
            return
        fired["value"] = True
        try:
            manager.async_start_scan("pve1", 200, target_is_running=True)
        except PackageScanError as err:
            attempts["scan"] = err
        else:
            attempts["scan"] = None
        try:
            manager.async_start_health("pve1", 200, target_is_running=True)
        except PackageScanError as err:
            attempts["health"] = err
        else:
            attempts["health"] = None
        try:
            manager.async_start_autoremove(
                "pve1", 200, target_is_running=True, snapshot_permission=True
            )
        except PackageUpdateError as err:
            attempts["autoremove"] = err
        else:
            attempts["autoremove"] = None

    manager, _proxmox = _update_manager(
        hass, mock_config_entry, transport, on_state_change=on_state_change
    )
    manager_holder.append(manager)
    await _review_target(manager, "pve1", 200)

    patches = _update_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )

    assert fired["value"] is True
    assert set(attempts) == {"scan", "health", "autoremove"}
    assert isinstance(attempts["scan"], PackageScanError)
    assert attempts["scan"].failure is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert isinstance(attempts["health"], PackageScanError)
    assert attempts["health"].failure is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert isinstance(attempts["autoremove"], PackageUpdateError)
    assert attempts["autoremove"].outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY

    # Update is still SUCCESS, and exactly one Health claim -- the post-op
    # one -- survives.
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    health = manager.health_record("pve1", 200)
    assert health.source is HealthSource.UPDATE
    assert health.check_status in (
        HealthCheckStatus.RUNNING,
        HealthCheckStatus.COMPLETED,
    )


async def test_reentrant_operations_during_autoremove_success_publish_are_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The same re-entrancy protection holds for the post-Autoremove hand-off."""
    candidates = _candidates(1)
    transport = CleanupTransport([_parsed(candidates), _parsed(candidates)])
    manager_holder: list[PackageManager] = []
    attempts: dict[str, Exception | None] = {}
    fired = {"value": False}

    def on_state_change() -> None:
        if not manager_holder or fired["value"]:
            return
        manager = manager_holder[0]
        if manager.cleanup_record("pve1", 200).status is not (
            PackageUpdateStatus.SUCCESS
        ):
            return
        fired["value"] = True
        try:
            manager.async_start_scan("pve1", 200, target_is_running=True)
        except PackageScanError as err:
            attempts["scan"] = err
        else:
            attempts["scan"] = None
        try:
            manager.async_start_health("pve1", 200, target_is_running=True)
        except PackageScanError as err:
            attempts["health"] = err
        else:
            attempts["health"] = None
        try:
            manager.async_start_update(
                "pve1", 200, target_is_running=True, snapshot_permission=True
            )
        except PackageUpdateError as err:
            attempts["update"] = err
        else:
            attempts["update"] = None

    manager, _proxmox = _cleanup_manager(
        hass, mock_config_entry, transport, on_state_change=on_state_change
    )
    manager_holder.append(manager)
    await _cleanup_scan(manager)

    patches = _cleanup_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        task = manager._cleanup_tasks[("pve1", 200)]  # noqa: SLF001
        await task

    assert fired["value"] is True
    assert set(attempts) == {"scan", "health", "update"}
    assert isinstance(attempts["scan"], PackageScanError)
    assert attempts["scan"].failure is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert isinstance(attempts["health"], PackageScanError)
    assert attempts["health"].failure is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert isinstance(attempts["update"], PackageUpdateError)
    assert attempts["update"].outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY

    assert manager.cleanup_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    health = manager.health_record("pve1", 200)
    assert health.source is HealthSource.AUTOREMOVE
    assert health.check_status in (
        HealthCheckStatus.RUNNING,
        HealthCheckStatus.COMPLETED,
    )


# ---------------------------------------------------------------------------
# F2: NO HEALTH FROM UNLOAD-CANCELLATION BRANCHES
# ---------------------------------------------------------------------------


async def test_cancelled_update_during_post_success_observation_starts_no_health(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Cancelling Update mid post-success observation never starts Health.

    This is the config-entry unload/reload shape: HA cancels the tracked
    background task while it is waiting inside the post-success cleanup
    observation. The mutation must still terminalize SUCCESS exactly as
    before, but no new Health diagnostic work may start and escape the
    unload.
    """
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    # Gate only the post-mutation observation, not the scan-phase call
    # _review_target already made.
    baseline_calls = transport.plan_autoremove_calls
    transport.plan_autoremove_release.clear()

    patches = _update_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        task = manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        await asyncio.wait_for(
            _wait_for_calls_beyond(
                lambda: transport.plan_autoremove_calls, baseline_calls
            ),
            1,
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.SUCCESS
    assert record.outcome is PackageUpdateOutcome.SUCCESS
    assert manager.health_record("pve1", 200) == PackageHealthRecord()
    assert ("pve1", 200) not in manager._health_tasks  # noqa: SLF001
    assert transport.health_calls == 0


async def _wait_for_calls_beyond(count: Callable[[], int], baseline: int) -> None:
    """Wait until a call counter advances past its captured baseline."""
    while count() <= baseline:
        await asyncio.sleep(0)


async def test_cancelled_autoremove_during_post_success_observation_starts_no_health(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The same unload-safety guard holds for the post-Autoremove observation."""
    candidates = _candidates(1)
    transport = CleanupTransport(
        [_parsed(candidates), _parsed(candidates), _parsed(candidates)]
    )
    transport.gate_plan_autoremove_call = 3
    manager, _proxmox = _cleanup_manager(hass, mock_config_entry, transport)
    await _cleanup_scan(manager)

    patches = _cleanup_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        task = manager._cleanup_tasks[("pve1", 200)]  # noqa: SLF001
        await asyncio.wait_for(transport.gated_plan_autoremove_entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    record = manager.cleanup_record("pve1", 200)
    assert record.status is PackageUpdateStatus.SUCCESS
    assert record.outcome is PackageUpdateOutcome.SUCCESS
    assert manager.health_record("pve1", 200) == PackageHealthRecord()
    assert ("pve1", 200) not in manager._health_tasks  # noqa: SLF001
    assert transport.health_calls == 0


# ---------------------------------------------------------------------------
# POST-UPDATE / POST-AUTOREMOVE HAND-OFF
# ---------------------------------------------------------------------------


async def test_update_success_claims_health_running_in_the_same_handoff(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Update becomes terminal SUCCESS, notifies, and claims Health RUNNING as one turn."""
    transport = UpdateTransport()
    complete = MagicMock()
    manager, _proxmox = _update_manager(
        hass, mock_config_entry, transport, complete_callback=complete
    )
    await _review_target(manager, "pve1", 200)
    # Hold the just-claimed Health check open so its RUNNING claim can be
    # observed distinctly from Update's own already-terminal SUCCESS.
    transport.health_release.clear()

    patches = _update_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )

        record = manager.update_record("pve1", 200)
        assert record.status is PackageUpdateStatus.SUCCESS
        complete.assert_called_once()
        assert complete.call_args.args[2].status is PackageUpdateStatus.SUCCESS

        health = manager.health_record("pve1", 200)
        assert health.check_status is HealthCheckStatus.RUNNING
        assert health.source is HealthSource.UPDATE

        transport.health_release.set()
        await manager._health_tasks[("pve1", 200)]  # noqa: SLF001

    assert transport.health_calls == 1
    assert manager.health_record("pve1", 200).check_status is (
        HealthCheckStatus.COMPLETED
    )


async def test_failed_update_does_not_start_health(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A failed Update never claims or runs Health."""
    transport = UpdateTransport()
    transport.mutation = PackageUpdateError(
        PackageUpdateOutcome.MUTATION_FAILED, "boom"
    )
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)

    patches = _update_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )

    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.FAILED
    assert manager.health_record("pve1", 200) == PackageHealthRecord()
    assert transport.health_calls == 0


async def test_health_outcome_never_changes_the_published_update_outcome(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """FAILED Health evidence never retroactively changes Update SUCCESS."""
    transport = UpdateTransport()
    transport.health_outcome = PackageHealthOutcome(
        HealthState.FAILED,
        HealthReason.DPKG_INTERRUPTED,
        PackageHealthEvidence(
            guest_exec=True,
            guest_exec_unavailable=False,
            dpkg=HealthDpkgState.INTERRUPTED,
            unfinished_package_count=3,
            reboot_required=None,
        ),
    )
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)

    patches = _update_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )

    # The default (unreleased) transport health check resolves eagerly, so
    # no stale entry is left in _health_tasks (see the F5 regression test).
    assert ("pve1", 200) not in manager._health_tasks  # noqa: SLF001
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert manager.health_record("pve1", 200).state is HealthState.FAILED


async def test_health_cancellation_after_handoff_never_changes_update_outcome(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Cancelling the just-claimed Health task leaves Update SUCCESS untouched."""
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    transport.health_release.clear()

    patches = _update_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        health_task = manager._health_tasks[("pve1", 200)]  # noqa: SLF001
        health_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await health_task

    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS


async def test_health_task_creation_failure_never_changes_update_outcome(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A failure creating the Health task removes only the RUNNING claim.

    The fake deliberately does NOT close the coroutine itself: the
    implementation must own and close it explicitly (F4), never leaving an
    un-awaited coroutine behind.
    """
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    original_create = mock_config_entry.async_create_background_task
    captured: dict[str, Any] = {}

    def failing_create(hass_arg, coro, name, *args, **kwargs):
        if name.startswith("package health check"):
            captured["coro"] = coro
            raise RuntimeError("no background task slots")
        return original_create(hass_arg, coro, name, *args, **kwargs)

    patches = _update_snapshot_patches()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patch.object(
            mock_config_entry,
            "async_create_background_task",
            side_effect=failing_create,
        ),
    ):
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )

    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert manager.health_record("pve1", 200) == PackageHealthRecord()
    assert ("pve1", 200) not in manager._health_tasks  # noqa: SLF001
    assert captured["coro"].cr_frame is None


async def test_failed_autoremove_does_not_start_health(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A failed Autoremove never claims or runs Health."""
    candidates = _candidates(1)
    transport = CleanupTransport([_parsed(candidates), _parsed(candidates)])
    transport.autoremove_result = PackageUpdateError(
        PackageUpdateOutcome.MUTATION_FAILED, "boom"
    )
    manager, _proxmox = _cleanup_manager(hass, mock_config_entry, transport)
    await _cleanup_scan(manager)

    patches = _cleanup_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        task = manager._cleanup_tasks[("pve1", 200)]  # noqa: SLF001
        await task

    assert manager.cleanup_record("pve1", 200).status is PackageUpdateStatus.FAILED
    assert manager.health_record("pve1", 200) == PackageHealthRecord()
    assert transport.health_calls == 0


async def test_autoremove_success_claims_health_running_in_the_same_handoff(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A successful Autoremove claims Health RUNNING in the same synchronous turn."""
    candidates = _candidates(1)
    transport = CleanupTransport([_parsed(candidates), _parsed(candidates)])
    transport.health_release.clear()
    manager, _proxmox = _cleanup_manager(hass, mock_config_entry, transport)
    await _cleanup_scan(manager)

    patches = _cleanup_snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        manager.async_start_autoremove(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        task = manager._cleanup_tasks[("pve1", 200)]  # noqa: SLF001
        await task

        assert manager.cleanup_record("pve1", 200).status is (
            PackageUpdateStatus.SUCCESS
        )
        health = manager.health_record("pve1", 200)
        assert health.check_status is HealthCheckStatus.RUNNING
        assert health.source is HealthSource.AUTOREMOVE

        transport.health_release.set()
        await manager._health_tasks[("pve1", 200)]  # noqa: SLF001


# ---------------------------------------------------------------------------
# ENTITY / HA
# ---------------------------------------------------------------------------


def _set_container_status(mock_proxmox_client: MagicMock, status: str) -> None:
    """Set CT200's current coordinator observation."""
    containers = deepcopy(mock_proxmox_client._node_mock.lxc.get.return_value)  # noqa: SLF001
    for container in containers:
        if container["vmid"] == "200":
            container["status"] = status
    mock_proxmox_client._node_mock.lxc.get.return_value = containers  # noqa: SLF001


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    """Press one native Home Assistant button."""
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_health_entities_exist_only_with_configured_package_transport(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Health button/sensor follow the same package-eligibility boundary."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get(HEALTH_BUTTON) is None
    assert hass.states.get(HEALTH_SENSOR) is None


async def test_health_entities_exist_and_start_never_with_bounded_attributes(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A fresh package-eligible LXC has Health entities in the never state."""
    await setup_integration(hass, mock_config_entry)
    button_state = hass.states.get(HEALTH_BUTTON)
    sensor_state = hass.states.get(HEALTH_SENSOR)
    assert button_state is not None
    assert button_state.state != STATE_UNAVAILABLE
    assert sensor_state is not None
    assert sensor_state.state == STATE_UNKNOWN
    assert sensor_state.attributes["check_status"] == "never"


async def test_health_button_unavailable_when_stopped(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A stopped guest cannot start a manual Health check."""
    _set_container_status(mock_proxmox_client, "stopped")
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get(HEALTH_BUTTON).state == STATE_UNAVAILABLE


async def test_health_button_unavailable_while_scan_is_running(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A running Scan makes the Health button unavailable for that VMID."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_scan(_self, _node, _vmid):
        entered.set()
        await release.wait()
        return RESULT

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=gated_scan,
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN_BUTTON)
        await entered.wait()
        assert hass.states.get(HEALTH_BUTTON).state == STATE_UNAVAILABLE
        release.set()
        await hass.async_block_till_done()


async def test_package_action_buttons_unavailable_while_health_is_running(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Scan/Update/Autoremove mirror the backend Health busy rejection.

    Presentation only: the manager's own busy checks stay authoritative.
    A Health attempt for a different VMID leaves these buttons unaffected.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_health(_self, _node, _vmid):
        entered.set()
        await release.wait()
        return _default_health_outcome()

    actions = (SCAN_BUTTON, UPDATE_BUTTON, AUTOREMOVE_BUTTON)
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            AsyncMock(return_value=RESULT),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            AsyncMock(return_value=_parsed(_candidates(1))),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_check_health",
            new=gated_health,
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN_BUTTON)
        await hass.async_block_till_done()
        await _press(hass, REVIEW_BUTTON)
        await _press(hass, APPROVE_BUTTON)
        for entity_id in (*actions, HEALTH_BUTTON):
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

        manager = mock_config_entry.runtime_data.package_manager
        manager._health_records[("pve1", 201)] = PackageHealthRecord(  # noqa: SLF001
            check_status=HealthCheckStatus.RUNNING
        )
        manager._on_state_change()  # noqa: SLF001
        await hass.async_block_till_done()
        for entity_id in (*actions, HEALTH_BUTTON):
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
        manager._health_records.pop(("pve1", 201))  # noqa: SLF001

        await _press(hass, HEALTH_BUTTON)
        await entered.wait()
        for entity_id in (*actions, HEALTH_BUTTON):
            assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

        release.set()
        await hass.async_block_till_done()
        for entity_id in (*actions, HEALTH_BUTTON):
            assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_manual_health_press_returns_while_check_runs_in_background(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The Health button press does not block on the SSH round trip."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_health(_self, _node, _vmid):
        entered.set()
        await release.wait()
        return _default_health_outcome()

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_check_health",
        new=gated_health,
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, HEALTH_BUTTON)
        await entered.wait()
        assert hass.states.get(HEALTH_SENSOR).state == STATE_UNKNOWN
        assert hass.states.get(HEALTH_SENSOR).attributes["check_status"] == "running"
        # The button itself reflects the busy manager state while running;
        # the manager-level rejection of a concurrent Health start is
        # covered directly in the MANAGER tests above.
        assert hass.states.get(HEALTH_BUTTON).state == STATE_UNAVAILABLE

        release.set()
        await hass.async_block_till_done()
        assert hass.states.get(HEALTH_SENSOR).state == "healthy"
        assert hass.states.get(HEALTH_SENSOR).attributes["check_status"] == (
            "completed"
        )


@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        (
            PackageHealthOutcome(
                HealthState.HEALTHY,
                None,
                PackageHealthEvidence(True, False, HealthDpkgState.OK, None, None),
            ),
            "healthy",
        ),
        (
            PackageHealthOutcome(
                HealthState.DEGRADED,
                None,
                PackageHealthEvidence(True, False, HealthDpkgState.OK, None, True),
            ),
            "degraded",
        ),
        (
            PackageHealthOutcome(
                HealthState.FAILED,
                HealthReason.DPKG_INTERRUPTED,
                PackageHealthEvidence(
                    True, False, HealthDpkgState.INTERRUPTED, 2, None
                ),
            ),
            "failed",
        ),
        (
            PackageHealthError(HealthReason.GUEST_UNAVAILABLE, "gone"),
            STATE_UNKNOWN,
        ),
    ],
)
async def test_health_sensor_reflects_every_terminal_state(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    outcome: PackageHealthOutcome | Exception,
    expected_state: str,
) -> None:
    """Every classified verdict, and UNKNOWN, is reflected as native_value."""
    side_effect = outcome if isinstance(outcome, Exception) else None
    return_value = None if isinstance(outcome, Exception) else outcome
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_check_health",
        AsyncMock(side_effect=side_effect, return_value=return_value),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, HEALTH_BUTTON)
        await hass.async_block_till_done()
    state = hass.states.get(HEALTH_SENSOR)
    assert state.state == expected_state
    assert set(state.attributes) <= {
        "friendly_name",
        "device_class",
        "options",
        "check_status",
        "checked_at",
        "source",
        "reason",
        "guest_exec",
        "dpkg",
        "unfinished_package_count",
        "reboot_required",
    }
    for forbidden in (
        "pve_status",
        "cpu",
        "ram",
        "uptime",
        "systemd",
        "package_name",
        "packages",
    ):
        assert forbidden not in state.attributes


async def test_health_produces_no_notifications_and_no_coordinator_refresh(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The sensor is the only surface; Health never triggers a coordinator refresh."""
    failed = PackageHealthOutcome(
        HealthState.FAILED,
        HealthReason.DPKG_INTERRUPTED,
        PackageHealthEvidence(True, False, HealthDpkgState.INTERRUPTED, 1, None),
    )
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_check_health",
        AsyncMock(return_value=failed),
    ):
        await setup_integration(hass, mock_config_entry)
        coordinator = mock_config_entry.runtime_data
        coordinator.async_request_refresh = AsyncMock()
        before = set(pn._async_get_or_create_notifications(hass))  # noqa: SLF001
        await _press(hass, HEALTH_BUTTON)
        await hass.async_block_till_done()
    after = set(pn._async_get_or_create_notifications(hass))  # noqa: SLF001
    assert after == before
    coordinator.async_request_refresh.assert_not_awaited()
    assert hass.states.get(HEALTH_SENSOR).state == "failed"


async def test_health_result_does_not_survive_reload(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A completed Health result is gone after reload; nothing is restored."""
    healthy = _default_health_outcome()
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_check_health",
        AsyncMock(return_value=healthy),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, HEALTH_BUTTON)
        await hass.async_block_till_done()
    assert hass.states.get(HEALTH_SENSOR).state == "healthy"

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(HEALTH_SENSOR)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["check_status"] == "never"
