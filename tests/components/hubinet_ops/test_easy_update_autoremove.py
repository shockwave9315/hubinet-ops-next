"""YOLO continuation: blueprint-equivalent semantics over the real manager."""

# ruff: noqa: SLF001 -- inspect existing ephemeral records and entry tasks

import asyncio
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops import easy_update
from custom_components.hubinet_ops.easy_update import (
    EasyUpdateTarget,
    async_start_post_update_autoremove,
)
from custom_components.hubinet_ops.packages.models import (
    CleanupEvidence,
    HealthCheckStatus,
    HealthState,
    PackageHealthOutcome,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateStatus,
)
from homeassistant.core import HomeAssistant

from .test_package_cleanup import (
    NOW,
    CleanupTransport,
    _candidates,
    _manager,
    _parsed,
    _review,
    _scan,
    _snapshot_patches,
)


class FakeCoordinator:
    """Only the coordinator surface the continuation reads."""

    def __init__(self) -> None:
        """Expose one running package-node LXC with VM.Snapshot."""
        self.listeners: list = []
        self.package_manager = None
        self.data = {"pve1": SimpleNamespace(containers={200: {"status": "running"}})}
        self.permissions = {"/": {"VM.Snapshot": 1}}

    def async_add_listener(self, update_callback):
        """Register a no-argument listener like DataUpdateCoordinator."""
        self.listeners.append(update_callback)
        return lambda: self.listeners.remove(update_callback)

    def notify(self) -> None:
        """Fan out the manager's state-change callback."""
        for listener in list(self.listeners):
            listener()


async def _updated(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    transport: CleanupTransport,
):
    """Scan, approve, start the real Update, then start the continuation."""
    entry.add_to_hass(hass)
    coordinator = FakeCoordinator()
    manager, _ = _manager(hass, entry, transport, on_state_change=coordinator.notify)
    coordinator.package_manager = manager
    await _scan(manager)
    await _review(manager)
    manager.async_start_update(
        "pve1", 200, target_is_running=True, snapshot_permission=True
    )
    target = EasyUpdateTarget(entry, coordinator, "pve1", 200)
    async_start_post_update_autoremove(hass, target, manager.update_record("pve1", 200))
    return manager, coordinator


@contextmanager
def _patched(*extra):
    with ExitStack() as stack:
        for item in (*_snapshot_patches(), *extra):
            stack.enter_context(item)
        yield


def _held_health() -> CleanupTransport:
    """Scan sees nothing; post-Update and Autoremove's fresh plan see seven."""
    fresh = _parsed(_candidates(7))
    transport = CleanupTransport([_parsed(()), fresh, fresh, _parsed(())])
    transport.ping_results = deque([True, True])  # Update and Autoremove liveness.
    transport.health_release.clear()
    return transport


async def _finish(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize(
    "health",
    [HealthState.HEALTHY, HealthState.DEGRADED, HealthState.FAILED, None],
)
async def test_autoremove_waits_for_health_then_runs_once_regardless_of_result(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, health
) -> None:
    """Health only has to stop running; its result is not a gate."""
    transport = _held_health()
    transport.health_outcome = PackageHealthOutcome(health, None, None)
    with _patched():
        manager, coordinator = await _updated(hass, mock_config_entry, transport)
        await transport.health_entered.wait()
        assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
        assert (
            manager.health_record("pve1", 200).check_status is HealthCheckStatus.RUNNING
        )
        await asyncio.sleep(0)
        assert transport.autoremove_calls == 0
        transport.health_release.set()
        await _finish(hass)
    assert transport.update_calls == 1
    assert transport.autoremove_calls == 1
    assert manager.cleanup_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert coordinator.listeners == []


@pytest.mark.parametrize(
    "failure",
    [
        PackageUpdateError(PackageUpdateOutcome.MUTATION_FAILED, "apt failed"),
        PackageUpdateError(PackageUpdateOutcome.MUTATION_UNCERTAIN, "uncertain"),
        PackageUpdateError(PackageUpdateOutcome.LIVENESS_FAILED, "no pong"),
    ],
)
async def test_failed_or_uncertain_update_never_autoremoves(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, failure
) -> None:
    """Only the same attempt's SUCCESS can lead to cleanup."""
    transport = _held_health()

    async def failing_update(_node, _vmid):
        transport.update_calls += 1
        raise failure

    transport.async_update = failing_update
    with _patched():
        manager, coordinator = await _updated(hass, mock_config_entry, transport)
        await _finish(hass)
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.FAILED
    assert transport.autoremove_calls == 0
    assert coordinator.listeners == []


@pytest.mark.parametrize("observation", [_parsed(()), RuntimeError("observe failed")])
async def test_no_fresh_positive_candidates_means_no_autoremove(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, observation
) -> None:
    """Empty or failed post-Update observation stops the continuation."""
    transport = CleanupTransport([_parsed(()), observation])
    with _patched():
        manager, _ = await _updated(hass, mock_config_entry, transport)
        await _finish(hass)
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert transport.autoremove_calls == 0


async def test_stale_cleanup_evidence_is_not_fresh(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Evidence observed before this Update attempt never authorizes cleanup."""
    transport = _held_health()
    with _patched():
        manager, _ = await _updated(hass, mock_config_entry, transport)
        await transport.health_entered.wait()
        manager._cleanup_evidence["pve1", 200] = CleanupEvidence(
            candidates=_candidates(7), observed_at=NOW - timedelta(seconds=1)
        )
        transport.health_release.set()
        await _finish(hass)
    assert transport.autoremove_calls == 0


async def test_different_update_attempt_stops_the_continuation(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An old attempt's SUCCESS cannot authorize cleanup for a newer record."""
    transport = _held_health()
    with _patched():
        manager, _ = await _updated(hass, mock_config_entry, transport)
        await transport.health_entered.wait()
        record = manager.update_record("pve1", 200)
        manager._update_records["pve1", 200] = replace(
            record, last_attempt=NOW + timedelta(seconds=5)
        )
        transport.health_release.set()
        await _finish(hass)
    assert transport.autoremove_calls == 0


@pytest.mark.parametrize("change", ["stopped", "no_permission", "restore"])
async def test_existing_autoremove_conditions_still_apply(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, change
) -> None:
    """Running guest, VM.Snapshot and no Restore remain required."""
    transport = _held_health()
    with _patched():
        manager, coordinator = await _updated(hass, mock_config_entry, transport)
        await transport.health_entered.wait()
        if change == "stopped":
            coordinator.data["pve1"].containers[200]["status"] = "stopped"
        elif change == "no_permission":
            coordinator.permissions = {}
        else:
            manager._restore_reserved.add(("pve1", 200))
        transport.health_release.set()
        await _finish(hass)
    assert transport.autoremove_calls == 0
    assert coordinator.listeners == []


async def test_manual_autoremove_race_is_owned_by_the_manager(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A manual Autoremove first means the continuation never starts another."""
    transport = _held_health()
    with _patched():
        manager, coordinator = await _updated(hass, mock_config_entry, transport)
        await transport.health_entered.wait()
        started = []

        def manual_first() -> None:
            if (
                not started
                and manager.health_record("pve1", 200).check_status
                is HealthCheckStatus.COMPLETED
            ):
                started.append("manual")
                manager.async_start_autoremove(
                    "pve1", 200, target_is_running=True, snapshot_permission=True
                )

        coordinator.listeners.insert(0, manual_first)
        transport.health_release.set()
        await _finish(hass)
    assert started
    assert transport.autoremove_calls == 1


async def test_observation_bound_ends_without_backend_effect(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A timeout never starts Autoremove and never touches the Update."""
    transport = _held_health()
    with _patched(patch.object(easy_update, "AUTOREMOVE_OBSERVATION_SECONDS", 0.05)):
        manager, coordinator = await _updated(hass, mock_config_entry, transport)
        await transport.health_entered.wait()
        await asyncio.sleep(0.2)
        assert coordinator.listeners == []
        transport.health_release.set()
        await _finish(hass)
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert transport.autoremove_calls == 0


async def test_unload_cancels_the_continuation(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Entry unload cancels the wait; nothing is started afterwards."""
    transport = _held_health()
    with _patched():
        _, coordinator = await _updated(hass, mock_config_entry, transport)
        await transport.health_entered.wait()
        await mock_config_entry._async_process_on_unload(hass)
        assert coordinator.listeners == []
        transport.health_release.set()
        await hass.async_block_till_done()
    assert transport.autoremove_calls == 0
