"""Tests for package-manager state, concurrency, and Home Assistant entities."""

import asyncio
from collections.abc import Iterator
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.packages.manager import PackageManager
from custom_components.hubinet_ops.packages.models import (
    PackageMutationResult,
    PackageScanError,
    PackageScanFailure,
    PackageScanRecord,
    PackageScanResult,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
    PendingPackage,
)
from custom_components.hubinet_ops.packages.parser import (
    ParsedAptSimulation,
    ParsedAutoremoveSimulation,
)
from custom_components.hubinet_ops.packages.snapshots import (
    RetainedSnapshotSummary,
    SnapshotError,
)
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant

from . import setup_integration

ATTEMPTED_AT = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
RESULT = PackageScanResult(
    os_id="debian",
    os_version="12",
    packages=(
        PendingPackage("openssl", "amd64", "1.0", "1.1", "Debian-Security", True),
        PendingPackage("example", "amd64", "1.0", "1.1", None, None),
    ),
    reboot_required=True,
    not_upgraded_count=41,
)


class GateTransport:
    """Controllable package transport which records active concurrency."""

    configured = True
    observed_helper_version = None

    def __init__(self) -> None:
        """Initialize empty concurrency and lifecycle controls."""
        self.calls: list[tuple[str, int]] = []
        self.active = 0
        self.max_active = 0
        self.outcomes: dict[int, PackageScanResult | PackageScanError] = {}
        self._entered: dict[int, asyncio.Event] = {}
        self._release: dict[int, asyncio.Event] = {}
        self._completed: dict[int, asyncio.Event] = {}

    def entered(self, vmid: int) -> asyncio.Event:
        """Return the event set when a VMID reaches the transport."""
        return self._entered.setdefault(vmid, asyncio.Event())

    def release(self, vmid: int) -> None:
        """Allow one VMID's transport call to finish."""
        self._release.setdefault(vmid, asyncio.Event()).set()

    def rearm(self, vmid: int) -> None:
        """Reset a VMID's release gate so its next scan waits again.

        Home Assistant's eager task execution would otherwise run a scan
        whose release event is already set to completion synchronously,
        before ``async_start_scan`` even returns, which would defeat tests
        that need to observe a genuinely in-flight ``RUNNING`` attempt.
        """
        self._release[vmid] = asyncio.Event()
        self._entered[vmid] = asyncio.Event()

    def completed(self, vmid: int) -> asyncio.Event:
        """Return the event set after a VMID leaves the transport."""
        return self._completed.setdefault(vmid, asyncio.Event())

    async def async_scan(self, expected_node: str, vmid: int) -> PackageScanResult:
        """Wait for release before returning the configured outcome."""
        self.calls.append((expected_node, vmid))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered(vmid).set()
        try:
            await self._release.setdefault(vmid, asyncio.Event()).wait()
            outcome = self.outcomes.get(vmid, RESULT)
            if isinstance(outcome, PackageScanError):
                raise outcome
            return outcome
        finally:
            self.active -= 1
            self.completed(vmid).set()

    async def async_plan_autoremove(
        self, expected_node: str, vmid: int
    ) -> ParsedAutoremoveSimulation:
        """Return a successful empty cleanup observation."""
        return ParsedAutoremoveSimulation(packages=(), not_upgraded_count=0)


def _manager(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    transport: GateTransport,
    listener: MagicMock | None = None,
) -> PackageManager:
    return PackageManager(
        hass,
        mock_config_entry,
        transport=transport,
        on_state_change=listener or MagicMock(),
        now=lambda: ATTEMPTED_AT,
    )


async def _wait_for_state(hass: HomeAssistant, entity_id: str, expected: str) -> None:
    """Yield until a manager listener has published the expected state."""
    for _ in range(100):
        if (
            state := hass.states.get(entity_id)
        ) is not None and state.state == expected:
            return
        await asyncio.sleep(0)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == expected


async def _wait_for_scan_status(
    hass: HomeAssistant, entity_id: str, expected: str
) -> None:
    """Yield until a package sensor publishes the expected attempt status."""
    for _ in range(100):
        if (state := hass.states.get(entity_id)) is not None and state.attributes.get(
            "scan_status"
        ) == expected:
            return
        await asyncio.sleep(0)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes.get("scan_status") == expected


async def test_manager_rejects_duplicate_scan_for_same_vmid(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Only one scan for a VMID can be active, even under another node key."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    await asyncio.wait_for(transport.entered(200).wait(), 1)

    with pytest.raises(PackageScanError) as caught:
        manager.async_start_scan("pve2", 200, target_is_running=True)
    assert caught.value.failure is PackageScanFailure.PACKAGE_MANAGER_BUSY
    assert transport.calls == [("pve1", 200)]

    transport.release(200)
    await task
    assert manager.record("pve1", 200).status is PackageScanStatus.SUCCESS


async def test_manager_limits_package_transport_to_two_global_scans(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A third package scan waits without creating a worker or durable queue."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    tasks = [
        manager.async_start_scan("pve1", vmid, target_is_running=True)
        for vmid in (200, 201, 202)
    ]
    await asyncio.wait_for(transport.entered(200).wait(), 1)
    await asyncio.wait_for(transport.entered(201).wait(), 1)
    await asyncio.sleep(0)
    assert transport.calls == [("pve1", 200), ("pve1", 201)]
    assert transport.max_active == 2

    transport.release(200)
    await asyncio.wait_for(transport.entered(202).wait(), 1)
    transport.release(201)
    transport.release(202)
    await asyncio.gather(*tasks)
    assert transport.max_active == 2
    assert all(
        manager.record("pve1", vmid).status is PackageScanStatus.SUCCESS
        for vmid in (200, 201, 202)
    )


async def test_manager_records_running_completion_and_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Every attempt atomically replaces state and bounds its failure detail."""
    transport = GateTransport()
    listener = MagicMock()
    manager = _manager(hass, mock_config_entry, transport, listener)

    success_task = manager.async_start_scan("pve1", 200, target_is_running=True)
    assert manager.record("pve1", 200).status is PackageScanStatus.RUNNING
    assert manager.record("pve1", 200).result is None
    transport.release(200)
    await success_task
    success = manager.record("pve1", 200)
    assert success.status is PackageScanStatus.SUCCESS
    assert success.result is RESULT

    transport.outcomes[201] = PackageScanError(
        PackageScanFailure.METADATA_REFRESH_FAILED, "x" * 1000
    )
    failed_task = manager.async_start_scan("pve1", 201, target_is_running=True)
    transport.release(201)
    await failed_task
    failure = manager.record("pve1", 201)
    assert failure.status is PackageScanStatus.FAILED
    assert failure.result is None
    assert failure.failure is PackageScanFailure.METADATA_REFRESH_FAILED
    assert failure.error_message == "x" * 500
    assert listener.call_count == 5


async def test_review_fresh_success_gets_a_token_and_is_unreviewed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T1: a fresh successful scan carries a token and starts unreviewed."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task

    record = manager.record("pve1", 200)
    assert record.status is PackageScanStatus.SUCCESS
    assert record.token is not None
    assert record.reviewed is False


async def test_review_every_new_success_gets_a_fresh_token(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T2: identical rows still get a distinct token on each new success."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    first = manager.record("pve1", 200)

    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    second = manager.record("pve1", 200)

    assert second.status is PackageScanStatus.SUCCESS
    assert second.result == first.result
    assert second.token != first.token
    assert second.reviewed is False


async def test_review_new_scan_start_immediately_clears_prior_review(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T3: starting a new scan destroys the prior successful review at once."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    token = manager.record("pve1", 200).token
    assert manager.confirm_review("pve1", 200, token) is True

    transport.rearm(200)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    current = manager.record("pve1", 200)
    assert current.status is PackageScanStatus.RUNNING
    assert current.token is None
    assert current.reviewed is False

    transport.release(200)
    await task


async def test_review_failed_scan_leaves_no_active_review(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T4: a failed re-scan replaces a reviewed success with no token/review."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    token = manager.record("pve1", 200).token
    assert manager.confirm_review("pve1", 200, token) is True

    transport.outcomes[200] = PackageScanError(
        PackageScanFailure.SIMULATION_FAILED, "APT simulation failed"
    )
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task

    failed = manager.record("pve1", 200)
    assert failed.status is PackageScanStatus.FAILED
    assert failed.token is None
    assert failed.reviewed is False


async def test_review_stale_token_is_rejected_without_raising(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T5: a token from a superseded scan cannot confirm the new one."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    stale_token = manager.record("pve1", 200).token

    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task

    assert manager.confirm_review("pve1", 200, stale_token) is False
    assert manager.record("pve1", 200).reviewed is False


async def test_review_token_from_one_target_does_not_confirm_another(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T6: a token scoped to one LXC cannot confirm a different LXC."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task_ct106 = manager.async_start_scan("pve1", 106, target_is_running=True)
    transport.release(106)
    await task_ct106
    task_ct107 = manager.async_start_scan("pve1", 107, target_is_running=True)
    transport.release(107)
    await task_ct107

    token_ct107 = manager.record("pve1", 107).token
    assert manager.confirm_review("pve1", 106, token_ct107) is False
    assert manager.record("pve1", 106).reviewed is False


async def test_review_confirmation_preserves_result_and_token(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T7: confirming changes only `reviewed`; result and token are untouched."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    before = manager.record("pve1", 200)

    assert manager.confirm_review("pve1", 200, before.token) is True
    after = manager.record("pve1", 200)
    assert after.reviewed is True
    assert after.token == before.token
    assert after.result is before.result


async def test_review_confirming_twice_is_idempotent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T8: confirming an already-reviewed record with the same token succeeds again."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    token = manager.record("pve1", 200).token

    assert manager.confirm_review("pve1", 200, token) is True
    assert manager.confirm_review("pve1", 200, token) is True
    assert manager.record("pve1", 200).reviewed is True


async def test_review_confirm_during_in_flight_scan_does_not_disturb_it(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T9: confirming a stale token while a scan is running rejects cleanly.

    Confirmation only ever reads and replaces the currently stored record;
    it must not disturb the RUNNING record's own in-flight completion,
    proving `_finish_scan`'s object-identity guard survives a confirmation
    attempt racing it.
    """
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    stale_token = manager.record("pve1", 200).token

    transport.rearm(200)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    assert manager.record("pve1", 200).status is PackageScanStatus.RUNNING

    assert manager.confirm_review("pve1", 200, stale_token) is False
    assert manager.record("pve1", 200).status is PackageScanStatus.RUNNING

    transport.release(200)
    await task
    landed = manager.record("pve1", 200)
    assert landed.status is PackageScanStatus.SUCCESS
    assert landed.token is not None
    assert landed.token != stale_token
    assert landed.reviewed is False


async def test_review_empty_plan_cannot_be_confirmed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T10: an empty successful plan may be viewed but never confirmed."""
    transport = GateTransport()
    empty_result = PackageScanResult(
        os_id="debian",
        os_version="12",
        packages=(),
        reboot_required=None,
        not_upgraded_count=0,
    )
    transport.outcomes[200] = empty_result
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task

    record = manager.record("pve1", 200)
    assert record.status is PackageScanStatus.SUCCESS
    assert record.token is not None
    assert record.reviewed is False

    assert manager.confirm_review("pve1", 200, record.token) is False
    assert manager.record("pve1", 200).reviewed is False


async def test_review_prune_discards_review_with_the_record(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """T11: pruning a reviewed target discards its review with the record."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    token = manager.record("pve1", 200).token
    assert manager.confirm_review("pve1", 200, token) is True

    manager.async_prune(current_targets=set())
    assert manager.record("pve1", 200) == PackageScanRecord()


async def test_package_entities_are_absent_without_transport_material(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Controls and summaries are absent without in-entry trust material."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("button.ct_nginx_scan_pending_packages") is None
    assert hass.states.get("sensor.ct_nginx_pending_package_updates") is None


async def test_package_entities_are_limited_to_authenticated_local_node(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Remote-node LXCs retain native entities but receive no package controls."""
    mock_proxmox_client.nodes.get.return_value = mock_proxmox_client._all_nodes[:2]
    local_resource = mock_proxmox_client._node_mock
    remote_resource = MagicMock()
    remote_resource.qemu.get.return_value = []
    remote_resource.lxc.get.return_value = [
        {
            "vmid": "300",
            "name": "ct-remote",
            "status": "running",
            "maxmem": 1073741824,
            "cpus": 1,
            "mem": 536870912,
            "cpu": 0.05,
            "maxdisk": 21474836480,
            "disk": 1125899906,
            "uptime": 43200,
            "netin": 1048576,
            "netout": 524288,
        }
    ]
    remote_resource.storage.get.return_value = []
    remote_resource.tasks.get.return_value = []
    mock_proxmox_client.nodes.side_effect = lambda node: (
        local_resource if node == "pve1" else remote_resource
    )

    await setup_integration(hass, mock_config_entry)

    assert hass.states.get("sensor.ct_nginx_pending_package_updates") is not None
    assert hass.states.get("button.ct_nginx_scan_pending_packages") is not None
    assert hass.states.get("sensor.ct_remote_status") is not None
    assert hass.states.get("binary_sensor.ct_remote_status") is not None
    assert hass.states.get("sensor.ct_remote_pending_package_updates") is None
    assert hass.states.get("button.ct_remote_scan_pending_packages") is None


async def test_legacy_trust_files_are_not_runtime_inputs(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Version-4 runtime never discovers trust from files, even on reload."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("button.ct_nginx_scan_pending_packages") is None
    assert hass.states.get("sensor.ct_nginx_pending_package_updates") is None

    private_key = hass.config.path(".ssh/hubinet_ops")
    known_hosts = hass.config.path(".ssh/known_hosts")
    private_key = Path(private_key)
    known_hosts = Path(known_hosts)
    private_key.parent.mkdir(parents=True, exist_ok=True)
    private_key.write_text("test private key")
    known_hosts.write_text("test host key")
    try:
        coordinator = mock_config_entry.runtime_data
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("button.ct_nginx_scan_pending_packages") is None
        assert hass.states.get("sensor.ct_nginx_pending_package_updates") is None

        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("button.ct_nginx_scan_pending_packages") is None
        assert hass.states.get("sensor.ct_nginx_pending_package_updates") is None
    finally:
        private_key.unlink(missing_ok=True)
        known_hosts.unlink(missing_ok=True)
        with suppress(OSError):
            private_key.parent.rmdir()


async def test_prune_discards_records_for_vmids_no_longer_present(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """N4: a pruned VMID's scan record is gone, not stale evidence.

    CT200 scan succeeds; CT200 then disappears upstream (deleted). The
    record must not keep presenting the old count as current evidence.
    """
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task
    assert manager.record("pve1", 200).status is PackageScanStatus.SUCCESS

    manager.async_prune(current_targets=set())
    assert manager.record("pve1", 200) == PackageScanRecord()


async def test_prune_scopes_to_exactly_the_missing_targets(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Pruning does not disturb records for targets still present."""
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    transport.release(200)
    await task

    manager.async_prune(current_targets={("pve1", 200)})
    assert manager.record("pve1", 200).status is PackageScanStatus.SUCCESS


async def test_prune_prevents_a_stale_in_flight_scan_from_resurrecting_a_record(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """N4: an old in-flight scan cannot recreate a pruned/reused record.

    CT200 is deleted (and its record pruned) while a scan for it is still
    in flight. That attempt completing afterward -- successfully or not --
    must not resurrect a record for the pruned target, even if a new,
    unrelated CT200 has since been scanned again.
    """
    transport = GateTransport()
    manager = _manager(hass, mock_config_entry, transport)
    task = manager.async_start_scan("pve1", 200, target_is_running=True)
    await asyncio.wait_for(transport.entered(200).wait(), 1)

    manager.async_prune(current_targets=set())
    assert manager.record("pve1", 200) == PackageScanRecord()

    transport.release(200)
    with suppress(asyncio.CancelledError):
        await task
    assert manager.record("pve1", 200) == PackageScanRecord()


async def test_running_success_then_failure_drives_sensor_unknown_semantics(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Running and latest-failed attempts hide an earlier successful count."""
    transport = GateTransport()

    async def fake_scan(_self, expected_node, vmid):
        return await transport.async_scan(expected_node, vmid)

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=fake_scan,
    ):
        await setup_integration(hass, mock_config_entry)
        initial = hass.states.get("sensor.ct_nginx_pending_package_updates")
        assert initial is not None
        assert initial.state == "unknown"
        assert initial.attributes["scan_status"] == "never"

        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ct_nginx_scan_pending_packages"},
            blocking=True,
        )
        await asyncio.wait_for(transport.entered(200).wait(), 1)
        state = hass.states.get("sensor.ct_nginx_pending_package_updates")
        assert state is not None
        assert state.state == "unknown"
        assert state.attributes["scan_status"] == "running"
        assert state.attributes["running"] is True

        transport.release(200)
        await _wait_for_state(hass, "sensor.ct_nginx_pending_package_updates", "2")
        state = hass.states.get("sensor.ct_nginx_pending_package_updates")
        assert state is not None
        assert state.attributes["security_updates"] == 1
        assert state.attributes["unknown_security_updates"] == 1
        assert state.attributes["not_upgraded_count"] == 41
        assert state.attributes["reboot_required"] is True

        transport.outcomes[200] = PackageScanError(
            PackageScanFailure.SIMULATION_FAILED, "APT simulation failed"
        )
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ct_nginx_scan_pending_packages"},
            blocking=True,
        )
        await _wait_for_scan_status(
            hass, "sensor.ct_nginx_pending_package_updates", "failed"
        )
        state = hass.states.get("sensor.ct_nginx_pending_package_updates")
        assert state is not None
        assert state.attributes["scan_status"] == "failed"
        assert state.attributes["last_error"] == "simulation_failed"
        assert "security_updates" not in state.attributes
        assert "reboot_required" not in state.attributes


@pytest.mark.parametrize("package_count", [0, 10_000])
async def test_success_count_and_large_result_attributes_are_bounded(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    package_count: int,
) -> None:
    """Success exposes exact 0/N while exact rows remain manager-private."""
    large_result = PackageScanResult(
        os_id="debian",
        os_version="12",
        packages=tuple(
            PendingPackage(f"package-{index}", "amd64", "1", "2", None, None)
            for index in range(package_count)
        ),
        reboot_required=None,
        not_upgraded_count=9,
    )

    async def fake_scan(_self, _expected_node, _vmid):
        return large_result

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=fake_scan,
    ):
        await setup_integration(hass, mock_config_entry)
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ct_nginx_scan_pending_packages"},
            blocking=True,
        )
        await _wait_for_state(
            hass, "sensor.ct_nginx_pending_package_updates", str(package_count)
        )

    state = hass.states.get("sensor.ct_nginx_pending_package_updates")
    assert state is not None
    attributes = dict(state.attributes)
    assert "packages" not in attributes
    assert len(json.dumps(attributes)) < 1000
    assert attributes["unknown_security_updates"] == package_count


async def test_in_flight_scan_does_not_block_native_proxmox_button(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The package trigger releases PARALLEL_UPDATES before SSH completes."""
    transport = GateTransport()

    async def fake_scan(_self, expected_node, vmid):
        return await transport.async_scan(expected_node, vmid)

    with (
        patch("custom_components.hubinet_ops.PLATFORMS", [Platform.BUTTON]),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            new=fake_scan,
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ct_nginx_scan_pending_packages"},
            blocking=True,
        )
        await asyncio.wait_for(transport.entered(200).wait(), 1)
        try:
            await asyncio.wait_for(
                hass.services.async_call(
                    "button",
                    SERVICE_PRESS,
                    {ATTR_ENTITY_ID: "button.ct_nginx_restart"},
                    blocking=True,
                ),
                1,
            )
        finally:
            transport.release(200)
            await asyncio.wait_for(transport.completed(200).wait(), 1)

    mock_proxmox_client.nodes("pve1").lxc(
        200
    ).status.reboot.post.assert_called_once_with()


async def test_stopped_lxc_invalidates_stored_package_count(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Leaving running invalidates evidence; restart remains unknown."""
    transport = GateTransport()
    zero_result = PackageScanResult(
        os_id="debian",
        os_version="12",
        packages=(),
        reboot_required=None,
        not_upgraded_count=0,
    )

    async def fake_scan(_self, expected_node, vmid):
        return await transport.async_scan(expected_node, vmid)

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=fake_scan,
    ):
        await setup_integration(hass, mock_config_entry)
        transport.outcomes[200] = zero_result
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ct_nginx_scan_pending_packages"},
            blocking=True,
        )
        await asyncio.wait_for(transport.entered(200).wait(), 1)
        transport.release(200)
        await _wait_for_state(hass, "sensor.ct_nginx_pending_package_updates", "0")

    button_state = hass.states.get("button.ct_nginx_scan_pending_packages")
    assert button_state is not None
    assert button_state.state != STATE_UNAVAILABLE

    # The guest stops.
    stopped_containers = deepcopy(
        mock_proxmox_client._node_mock.lxc.get.return_value  # noqa: SLF001
    )
    for container in stopped_containers:
        if container["vmid"] == "200":
            container["status"] = "stopped"
    mock_proxmox_client._node_mock.lxc.get.return_value = (  # noqa: SLF001
        stopped_containers
    )

    coordinator = mock_config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    sensor_state = hass.states.get("sensor.ct_nginx_pending_package_updates")
    assert sensor_state is not None
    assert sensor_state.state == STATE_UNAVAILABLE
    # The package button was already established to become unavailable
    # when its guest stops; confirm it still does alongside the sensor.
    button_state = hass.states.get("button.ct_nginx_scan_pending_packages")
    assert button_state is not None
    assert button_state.state == STATE_UNAVAILABLE
    # Accepted PR #11 architecture removes current package evidence as soon as
    # the target is observed outside the running state.
    assert coordinator.package_manager.record("pve1", 200) == PackageScanRecord()

    # The guest starts again; only an explicit Scan can establish fresh truth.
    running_containers = deepcopy(stopped_containers)
    for container in running_containers:
        if container["vmid"] == "200":
            container["status"] = "running"
    mock_proxmox_client._node_mock.lxc.get.return_value = (  # noqa: SLF001
        running_containers
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    sensor_state = hass.states.get("sensor.ct_nginx_pending_package_updates")
    assert sensor_state is not None
    assert sensor_state.state == STATE_UNKNOWN
    assert transport.calls == [("pve1", 200)]


async def test_stopped_lxc_discards_reviewed_state_and_requires_rescan(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A stopped LXC loses its review and starts again with unknown state."""
    transport = GateTransport()

    async def fake_scan(_self, expected_node, vmid):
        return await transport.async_scan(expected_node, vmid)

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=fake_scan,
    ):
        await setup_integration(hass, mock_config_entry)
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ct_nginx_scan_pending_packages"},
            blocking=True,
        )
        await asyncio.wait_for(transport.entered(200).wait(), 1)
        transport.release(200)
        await _wait_for_state(hass, "sensor.ct_nginx_pending_package_updates", "2")

    coordinator = mock_config_entry.runtime_data
    token = coordinator.package_manager.record("pve1", 200).token
    assert coordinator.package_manager.confirm_review("pve1", 200, token) is True

    state = hass.states.get("sensor.ct_nginx_pending_package_updates")
    assert state is not None
    assert state.attributes["reviewed"] is True

    # The guest stops.
    stopped_containers = deepcopy(
        mock_proxmox_client._node_mock.lxc.get.return_value  # noqa: SLF001
    )
    for container in stopped_containers:
        if container["vmid"] == "200":
            container["status"] = "stopped"
    mock_proxmox_client._node_mock.lxc.get.return_value = (  # noqa: SLF001
        stopped_containers
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    sensor_state = hass.states.get("sensor.ct_nginx_pending_package_updates")
    assert sensor_state is not None
    assert sensor_state.state == STATE_UNAVAILABLE
    record = coordinator.package_manager.record("pve1", 200)
    assert record == PackageScanRecord()
    assert coordinator.package_manager.confirm_review("pve1", 200, token) is False

    # The guest starts again with no review and no automatic scan.
    running_containers = deepcopy(stopped_containers)
    for container in running_containers:
        if container["vmid"] == "200":
            container["status"] = "running"
    mock_proxmox_client._node_mock.lxc.get.return_value = (  # noqa: SLF001
        running_containers
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    sensor_state = hass.states.get("sensor.ct_nginx_pending_package_updates")
    assert sensor_state is not None
    assert sensor_state.state == STATE_UNKNOWN
    assert "reviewed" not in sensor_state.attributes
    assert transport.calls == [("pve1", 200)]


class UpdateTransport:
    """Controllable transport for package-update orchestration tests."""

    configured = True

    def __init__(self) -> None:
        """Initialize successful defaults and optional plan blocking."""
        self.plan = ParsedAptSimulation(RESULT.packages, RESULT.not_upgraded_count)
        self.mutation: PackageMutationResult | PackageUpdateError = (
            PackageMutationResult(
                before={
                    ("openssl", "amd64"): "1.0",
                    ("example", "amd64"): "1.0",
                },
                after={
                    ("openssl", "amd64"): "1.2",
                    ("example", "amd64"): "1.1",
                },
            )
        )
        self.plan_entered = asyncio.Event()
        self.plan_release = asyncio.Event()
        self.plan_release.set()
        self.update_calls = 0
        self.ping_results: list[bool | PackageUpdateError] = [True]

    async def async_scan(self, expected_node: str, vmid: int) -> PackageScanResult:
        """Return a successful reviewable scan."""
        return RESULT

    async def async_plan(self, expected_node: str, vmid: int) -> ParsedAptSimulation:
        """Return the configured execution-time plan after an optional gate."""
        self.plan_entered.set()
        await self.plan_release.wait()
        return self.plan

    async def async_update(
        self, expected_node: str, vmid: int
    ) -> PackageMutationResult:
        """Return or raise the configured mutation outcome."""
        self.update_calls += 1
        if isinstance(self.mutation, PackageUpdateError):
            raise self.mutation
        return self.mutation

    async def async_ping(self, expected_node: str, vmid: int) -> bool:
        """Return or raise the next fixed-command PONG outcome."""
        result = self.ping_results.pop(0)
        if isinstance(result, PackageUpdateError):
            raise result
        return result


async def _review_target(manager: PackageManager, node: str, vmid: int) -> None:
    """Create and confirm one successful scan through public manager methods."""
    task = manager.async_start_scan(node, vmid, target_is_running=True)
    await task
    token = manager.record(node, vmid).token
    assert token is not None
    assert manager.confirm_review(node, vmid, token) is True


def _update_manager(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    transport: UpdateTransport,
    *,
    proxmox: MagicMock | None = None,
    sleep: AsyncMock | None = None,
    retained_callback: MagicMock | None = None,
    complete_callback: MagicMock | None = None,
) -> tuple[PackageManager, MagicMock]:
    """Build an update-capable manager and native PVE status double."""
    proxmox = proxmox or MagicMock()
    proxmox.nodes.return_value.lxc.return_value.status.current.get.return_value = {
        "status": "running"
    }
    return (
        PackageManager(
            hass,
            mock_config_entry,
            transport=transport,
            on_state_change=MagicMock(),
            now=lambda: ATTEMPTED_AT,
            proxmox_getter=lambda: proxmox,
            sleep=sleep or AsyncMock(),
            on_retained_snapshots=retained_callback or MagicMock(),
            on_update_complete=complete_callback or MagicMock(),
        ),
        proxmox,
    )


def _snapshot_patches(*, old_rows: object = ()):
    """Patch native lifecycle at the manager boundary for orchestration tests."""
    return (
        patch(
            "custom_components.hubinet_ops.packages.manager.async_list_snapshots",
            new=AsyncMock(return_value=old_rows),
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
            return_value="hubinet-preupd-20260908120000-ab12cd",
        ),
    )


@pytest.mark.parametrize(
    "packages",
    [
        RESULT.packages
        + (PendingPackage("gained", "amd64", "1", "2", None, None),),
        RESULT.packages[:1],
        (
            PendingPackage(
                "openssl", "amd64", "1.0", "1.2", "Debian-Security", True
            ),
            RESULT.packages[1],
        ),
        (
            PendingPackage(
                "openssl", "arm64", "1.0", "1.1", "Debian-Security", True
            ),
            RESULT.packages[1],
        ),
    ],
    ids=["gained", "disappeared", "candidate_changed", "architecture_changed"],
)
async def test_update_plan_changes_stop_before_snapshot_or_mutation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    packages: tuple[PendingPackage, ...],
) -> None:
    """Every equality-bearing plan change requires a normal review cycle."""
    transport = UpdateTransport()
    transport.plan = ParsedAptSimulation(packages, 0)
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)

    list_patch, create_patch, delete_patch, name_patch = _snapshot_patches()
    with list_patch as list_snapshots, create_patch as create, delete_patch, name_patch:
        task = manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        assert manager.record("pve1", 200) == PackageScanRecord()
        await task

    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.FAILED
    assert record.outcome is PackageUpdateOutcome.PLAN_CHANGED
    assert record.snapshot_retained is False
    assert transport.update_calls == 0
    list_snapshots.assert_not_awaited()
    create.assert_not_awaited()


async def test_metadata_only_plan_differences_do_not_fail_gate(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Origin/security remain informational and do not bear on equality."""
    transport = UpdateTransport()
    transport.plan = ParsedAptSimulation(
        tuple(replace(package, origin="changed", security=None) for package in RESULT.packages),
        99,
    )
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert transport.update_calls == 1


async def test_success_reports_actual_inventory_changes_and_deletes_current_snapshot(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Success counts observed version changes and cleans only current safety state."""
    transport = UpdateTransport()
    complete = MagicMock()
    retained = MagicMock()
    manager, _proxmox = _update_manager(
        hass,
        mock_config_entry,
        transport,
        retained_callback=retained,
        complete_callback=complete,
    )
    await _review_target(manager, "pve1", 200)
    old = "hubinet-preupd-20260907120000-cd34ef"
    patches = _snapshot_patches(old_rows=[{"name": old}, {"name": "manual"}])
    with patches[0], patches[1] as create, patches[2] as delete, patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.SUCCESS
    assert record.outcome is PackageUpdateOutcome.SUCCESS
    assert record.changed_package_count == 2
    assert record.liveness is True
    assert record.snapshot_retained is False
    retained.assert_called_once_with(
        "pve1", 200, RetainedSnapshotSummary(total_count=1, names=(old,))
    )
    create.assert_awaited_once()
    delete.assert_awaited_once()
    assert delete.await_args.args[3] == "hubinet-preupd-20260908120000-ab12cd"
    complete.assert_called_once_with("pve1", 200, record)


async def test_newer_post_gate_version_is_not_a_failure(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """After a passing gate, installed 1.2 is not rejected against reviewed 1.1."""
    transport = UpdateTransport()
    transport.mutation = PackageMutationResult(
        before={("openssl", "amd64"): "1.0"},
        after={("openssl", "amd64"): "1.2"},
    )
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.SUCCESS
    assert record.changed_package_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        PackageUpdateError(
            PackageUpdateOutcome.PACKAGE_MANAGER_BUSY, "APT or dpkg is busy"
        ),
        PackageUpdateError(PackageUpdateOutcome.MUTATION_FAILED, "APT returned 100"),
        PackageUpdateError(PackageUpdateOutcome.MUTATION_TIMED_OUT, "timed out"),
        PackageUpdateError(PackageUpdateOutcome.MUTATION_UNCERTAIN, "uncertain"),
    ],
)
async def test_mutation_failures_retain_confirmed_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    failure: PackageUpdateError,
) -> None:
    """No failed, timed-out, busy, or uncertain mutation deletes safety state."""
    transport = UpdateTransport()
    transport.mutation = failure
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2] as delete, patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.FAILED
    assert record.outcome is failure.outcome
    assert record.snapshot_retained is True
    assert record.snapshot_uncertain is False
    assert record.snapshot_name == "hubinet-preupd-20260908120000-ab12cd"
    delete.assert_not_awaited()


async def test_snapshot_create_failure_prevents_mutation_and_preserves_uncertainty(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An unconfirmed native safety snapshot never permits apt mutation."""
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    patches = _snapshot_patches()
    with (
        patches[0],
        patch(
            "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
            new=AsyncMock(side_effect=SnapshotError("create uncertain", True)),
        ),
        patches[2] as delete,
        patches[3],
    ):
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.FAILED
    assert record.outcome is PackageUpdateOutcome.SNAPSHOT_FAILED
    assert record.snapshot_retained is False
    assert record.snapshot_uncertain is True
    assert record.snapshot_name == "hubinet-preupd-20260908120000-ab12cd"
    assert transport.update_calls == 0
    delete.assert_not_awaited()


async def test_cancellation_before_snapshot_post_claims_no_snapshot(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Cancellation before the native POST reports neither retained nor uncertain."""
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    before_submit = asyncio.Event()

    async def blocked_before_submit(*_args, **_kwargs) -> None:
        before_submit.set()
        await asyncio.Event().wait()

    patches = _snapshot_patches()
    with (
        patches[0],
        patch(
            "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
            new=blocked_before_submit,
        ),
        patches[2] as delete,
        patches[3],
    ):
        task = manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        await before_submit.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    record = manager.update_record("pve1", 200)
    assert record.outcome is PackageUpdateOutcome.SNAPSHOT_FAILED
    assert record.snapshot_retained is False
    assert record.snapshot_uncertain is False
    assert record.snapshot_name is None
    assert transport.update_calls == 0
    delete.assert_not_awaited()


async def test_cancellation_after_snapshot_submission_is_uncertain(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Cancellation after entering native POST but before readiness is uncertain."""
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    after_submit = asyncio.Event()

    async def blocked_after_submit(*_args, **kwargs) -> None:
        kwargs["on_submit"]()
        after_submit.set()
        await asyncio.Event().wait()

    patches = _snapshot_patches()
    with (
        patches[0],
        patch(
            "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
            new=blocked_after_submit,
        ),
        patches[2] as delete,
        patches[3],
    ):
        task = manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        await after_submit.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    record = manager.update_record("pve1", 200)
    assert record.outcome is PackageUpdateOutcome.SNAPSHOT_FAILED
    assert record.snapshot_retained is False
    assert record.snapshot_uncertain is True
    assert record.snapshot_name == "hubinet-preupd-20260908120000-ab12cd"
    assert transport.update_calls == 0
    delete.assert_not_awaited()


async def test_pruned_interrupted_mutation_still_reports_retained_snapshot(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Target pruning discards state but still reports interrupted safety state."""
    transport = UpdateTransport()
    mutation_entered = asyncio.Event()

    async def blocked_mutation(_node: str, _vmid: int) -> PackageMutationResult:
        mutation_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    transport.async_update = blocked_mutation  # type: ignore[method-assign]
    complete = MagicMock()
    manager, _proxmox = _update_manager(
        hass, mock_config_entry, transport, complete_callback=complete
    )
    await _review_target(manager, "pve1", 200)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2] as delete, patches[3]:
        task = manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        await mutation_entered.wait()
        manager.async_prune(current_targets=set())
        with pytest.raises(asyncio.CancelledError):
            await task

    assert manager.update_record("pve1", 200) == PackageUpdateRecord()
    delete.assert_not_awaited()
    complete.assert_called_once()
    outcome = complete.call_args.args[2]
    assert outcome.outcome is PackageUpdateOutcome.MUTATION_UNCERTAIN
    assert outcome.snapshot_retained is True
    assert outcome.snapshot_uncertain is False
    assert outcome.snapshot_name == "hubinet-preupd-20260908120000-ab12cd"


async def test_liveness_retries_once_then_succeeds(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Native running plus transient /bin/true failure can still produce PONG."""
    transport = UpdateTransport()
    transport.ping_results = [
        PackageUpdateError(PackageUpdateOutcome.LIVENESS_FAILED, "transient"),
        True,
    ]
    sleep = AsyncMock()
    manager, _proxmox = _update_manager(
        hass, mock_config_entry, transport, sleep=sleep
    )
    await _review_target(manager, "pve1", 200)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    assert manager.update_record("pve1", 200).liveness is True
    sleep.assert_awaited_once_with(3.0)


@pytest.mark.parametrize("native_running", [True, False])
async def test_repeated_liveness_failure_or_stopped_native_status_retains_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    native_running: bool,
) -> None:
    """No PONG or a stopped native LXC leaves the exact safety snapshot."""
    transport = UpdateTransport()
    transport.ping_results = [False, False]
    manager, proxmox = _update_manager(hass, mock_config_entry, transport)
    if not native_running:
        proxmox.nodes.return_value.lxc.return_value.status.current.get.return_value = {
            "status": "stopped"
        }
    await _review_target(manager, "pve1", 200)
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2] as delete, patches[3]:
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.FAILED
    assert record.outcome is (
        PackageUpdateOutcome.LIVENESS_FAILED
        if native_running
        else PackageUpdateOutcome.GUEST_UNAVAILABLE
    )
    assert record.liveness is False
    assert record.snapshot_retained is True
    delete.assert_not_awaited()


async def test_cleanup_failure_preserves_success_and_reports_retained_name(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Cleanup failure is orthogonal to successful mutation and liveness."""
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    patches = _snapshot_patches()
    with (
        patches[0],
        patches[1],
        patch(
            "custom_components.hubinet_ops.packages.manager.async_delete_snapshot",
            new=AsyncMock(side_effect=SnapshotError("delete failed", True)),
        ),
        patches[3],
    ):
        await manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.SUCCESS
    assert record.outcome is PackageUpdateOutcome.SUCCESS
    assert record.liveness is True
    assert record.snapshot_cleanup_failed is True
    assert record.snapshot_retained is True
    assert record.snapshot_name == "hubinet-preupd-20260908120000-ab12cd"


async def test_scan_update_same_vmid_exclusion_and_unrelated_scan_progress(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Scan/update exclude by VMID while unrelated scan slots remain separate."""
    transport = UpdateTransport()
    manager, _proxmox = _update_manager(hass, mock_config_entry, transport)
    await _review_target(manager, "pve1", 200)
    transport.plan_release.clear()
    patches = _snapshot_patches()
    with patches[0], patches[1], patches[2], patches[3]:
        update_task = manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
        await transport.plan_entered.wait()
        with pytest.raises(PackageUpdateError) as duplicate:
            manager.async_start_update(
                "pve1", 200, target_is_running=True, snapshot_permission=True
            )
        assert duplicate.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY
        with pytest.raises(PackageScanError) as same_scan:
            manager.async_start_scan("pve1", 200, target_is_running=True)
        assert same_scan.value.failure is PackageScanFailure.PACKAGE_MANAGER_BUSY

        unrelated = manager.async_start_scan("pve1", 201, target_is_running=True)
        await unrelated
        assert manager.record("pve1", 201).status is PackageScanStatus.SUCCESS
        transport.plan_release.set()
        await update_task


async def test_update_while_same_vmid_scan_running_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An in-flight scan wins synchronous exclusion over stale review state."""
    transport = GateTransport()
    manager = PackageManager(
        hass,
        mock_config_entry,
        transport=transport,
        on_state_change=MagicMock(),
        proxmox_getter=MagicMock(),
    )
    scan = manager.async_start_scan("pve1", 200, target_is_running=True)
    await transport.entered(200).wait()
    with pytest.raises(PackageUpdateError) as caught:
        manager.async_start_update(
            "pve1", 200, target_is_running=True, snapshot_permission=True
        )
    assert caught.value.outcome is PackageUpdateOutcome.PACKAGE_MANAGER_BUSY
    transport.release(200)
    await scan
