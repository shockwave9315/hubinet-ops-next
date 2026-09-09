"""Tests for package-manager state, concurrency, and Home Assistant entities."""

import asyncio
from collections.abc import Iterator
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.packages.manager import PackageManager
from custom_components.hubinet_ops.packages.models import (
    PackageScanError,
    PackageScanFailure,
    PackageScanRecord,
    PackageScanResult,
    PackageScanStatus,
    PendingPackage,
)
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, Platform
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
    assert listener.call_count == 4


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


async def test_stopped_lxc_hides_stored_package_count_but_preserves_it(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """C5: a stopped guest must not keep exposing a stored exact count.

    An unsupported or unavailable guest is not equivalent to zero available
    updates (PRODUCT.md). The stored scan record itself is preserved, not
    invalidated, since packages cannot change while the container is
    stopped; only the sensor's (and, as already established, the button's)
    availability changes. The prior result becomes visible again, without
    a new scan, once the guest is running again.
    """
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

    # The guest starts again; the stored result becomes visible again
    # without a new scan being triggered.
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
    assert sensor_state.state == "0"
    assert transport.calls == [("pve1", 200)]


async def test_stopped_lxc_preserves_reviewed_state_and_resumes_without_rescan(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """T12: a stopped LXC keeps its stored review; starting shows it again.

    Stopping the LXC does not clear the manager's stored review or token;
    only the sensor's (and button's) availability changes while stopped.
    No automatic re-scan happens once it starts again.
    """
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
    assert record.reviewed is True
    assert record.token == token

    # The guest starts again; review becomes visible again, with no new scan.
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
    assert sensor_state.state == "2"
    assert sensor_state.attributes["reviewed"] is True
    assert transport.calls == [("pve1", 200)]
