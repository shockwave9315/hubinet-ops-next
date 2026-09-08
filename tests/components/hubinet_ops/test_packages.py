"""Tests for package-manager state, concurrency, and Home Assistant entities."""

import asyncio
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import (
    PACKAGE_SCAN_KNOWN_HOSTS,
    PACKAGE_SCAN_PRIVATE_KEY,
)
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
from homeassistant.const import ATTR_ENTITY_ID, Platform
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


@pytest.fixture
def package_transport_material(hass: HomeAssistant) -> Iterator[None]:
    """Provide and then remove the two local package transport prerequisites."""
    private_key = Path(hass.config.path(PACKAGE_SCAN_PRIVATE_KEY))
    known_hosts = Path(hass.config.path(PACKAGE_SCAN_KNOWN_HOSTS))
    private_key.parent.mkdir(parents=True, exist_ok=True)
    private_key.write_text("test private key")
    known_hosts.write_text("test host key")
    yield
    private_key.unlink(missing_ok=True)
    known_hosts.unlink(missing_ok=True)
    with suppress(OSError):
        private_key.parent.rmdir()


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


@pytest.mark.parametrize(
    "present_file", [None, PACKAGE_SCAN_PRIVATE_KEY, PACKAGE_SCAN_KNOWN_HOSTS]
)
async def test_package_entities_are_absent_without_transport_material(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    present_file: str | None,
) -> None:
    """Controls and summaries are not exposed when key or known_hosts is absent."""
    path = Path(hass.config.path(present_file)) if present_file is not None else None
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test material")
    try:
        await setup_integration(hass, mock_config_entry)
        assert hass.states.get("button.ct_nginx_scan_pending_packages") is None
        assert hass.states.get("sensor.ct_nginx_pending_package_updates") is None
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
            with suppress(OSError):
                path.parent.rmdir()


async def test_trust_files_added_without_reload_do_not_enable_entities(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """H2/N5: reload, not a live refresh, is the trust-material boundary.

    Trust material is evaluated once per config-entry setup. Creating both
    files afterward, without a reload, must not retroactively enable
    package entities -- even after a normal coordinator data refresh.
    Reloading (a fresh coordinator, and a fresh async_prepare) does.
    """
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("button.ct_nginx_scan_pending_packages") is None
    assert hass.states.get("sensor.ct_nginx_pending_package_updates") is None

    private_key = Path(hass.config.path(PACKAGE_SCAN_PRIVATE_KEY))
    known_hosts = Path(hass.config.path(PACKAGE_SCAN_KNOWN_HOSTS))
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
        assert hass.states.get("button.ct_nginx_scan_pending_packages") is not None
        assert (
            hass.states.get("sensor.ct_nginx_pending_package_updates") is not None
        )
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
