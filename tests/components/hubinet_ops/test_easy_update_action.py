"""Exercise the device-targeted Easy Update action over the real manager."""

# ruff: noqa: SLF001 -- seed and inspect existing ephemeral package records

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.packages.models import (
    HealthCheckStatus,
    PackageHealthRecord,
    PackageScanRecord,
    PackageScanResult,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateStatus,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import setup_integration
from .test_packages import RESULT

pytestmark = pytest.mark.usefixtures("mock_proxmox_client")

SCAN_AT = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
TOKEN = "exact-current-scan-token"


def _device_id(hass: HomeAssistant, identifier: str) -> str:
    device_id = dr.async_get_device_id_by_identifier(
        hass, (DOMAIN, identifier), config_entry_id="1234"
    )
    assert device_id is not None
    return device_id


def _seed_scan(entry: MockConfigEntry, vmid: int = 200, result=RESULT) -> None:
    entry.runtime_data.package_manager._set_record(
        "pve1",
        vmid,
        PackageScanRecord(
            status=PackageScanStatus.SUCCESS,
            last_attempt=SCAN_AT,
            result=result,
            token=TOKEN,
        ),
    )


async def _easy_update(hass: HomeAssistant, device_id: str, **data) -> None:
    await hass.services.async_call(
        DOMAIN, "easy_update", {"device_id": device_id, **data}, blocking=True
    )


async def _expect_invalid(hass: HomeAssistant, device_id: str, key: str, **data):
    with pytest.raises(ServiceValidationError) as err:
        await _easy_update(hass, device_id, **data)
    assert err.value.translation_key == key


@pytest.fixture
def blocked_plan():
    """Hold the background Update at its first transport step, then fail it."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def plan(_self, _node, _vmid):
        entered.set()
        await release.wait()
        raise PackageUpdateError(PackageUpdateOutcome.PLAN_FAILED, "test stop")

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_plan",
        new=plan,
    ):
        yield entered, release


async def test_device_resolves_to_exact_lxc_and_starts_existing_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    blocked_plan,
) -> None:
    """One click confirms the current token and starts the existing Update."""
    entered, release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    with patch.object(
        manager, "confirm_review", wraps=manager.confirm_review
    ) as confirm:
        await _easy_update(
            hass,
            _device_id(hass, "1234_container_200"),
            expected_scan_attempt=SCAN_AT.isoformat(),
        )
    confirm.assert_called_once_with("pve1", 200, TOKEN)
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.RUNNING
    # Accepted Update invalidates old scan evidence, as the button path does.
    assert manager.record("pve1", 200).status is PackageScanStatus.NEVER
    await entered.wait()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.FAILED


async def test_unknown_and_non_package_devices_are_rejected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """QEMU, nodes, other integrations, and other-node VMIDs never resolve."""
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    registry = dr.async_get(hass)
    await _expect_invalid(hass, "missing-device", "easy_update_unknown_device")
    native = [
        device
        for device in dr.async_entries_for_config_entry(
            registry, mock_config_entry.entry_id
        )
        if device.model in ("Node", "VM", "Storage")
    ]
    assert {device.model for device in native} == {"Node", "VM", "Storage"}
    for device in native:
        await _expect_invalid(hass, device.id, "easy_update_not_package_lxc")

    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    foreign = registry.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "1234_container_200")}
    )
    await _expect_invalid(hass, foreign.id, "easy_update_not_package_lxc")

    # A Hubinet container device whose VMID is not on the package node.
    other_node = registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "1234_container_300")},
    )
    await _expect_invalid(hass, other_node.id, "easy_update_not_package_lxc")


async def test_qemu_device_is_rejected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A QEMU VM device has no container identifier and is refused."""
    await setup_integration(hass, mock_config_entry)
    await _expect_invalid(
        hass, _device_id(hass, "1234_vm_100"), "easy_update_not_package_lxc"
    )


async def test_unloaded_or_unconfigured_entry_is_rejected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Only loaded entries with package transport can resolve a target."""
    await setup_integration(hass, mock_config_entry)
    device_id = _device_id(hass, "1234_container_200")
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await _expect_invalid(hass, device_id, "easy_update_not_package_lxc")


async def test_native_only_entry_has_no_package_target(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Without enrolled package transport the device is not package-capable."""
    await setup_integration(hass, mock_config_entry)
    await _expect_invalid(
        hass, _device_id(hass, "1234_container_200"), "easy_update_not_package_lxc"
    )


async def test_stopped_lxc_is_rejected_without_review(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A stopped package LXC resolves but is refused before any confirmation."""
    await setup_integration(hass, mock_config_entry)
    await _expect_invalid(
        hass, _device_id(hass, "1234_container_201"), "easy_update_not_running"
    )


@pytest.mark.parametrize(
    "record",
    [
        PackageScanRecord(),
        PackageScanRecord(status=PackageScanStatus.RUNNING, last_attempt=SCAN_AT),
        PackageScanRecord(status=PackageScanStatus.FAILED, last_attempt=SCAN_AT),
    ],
)
async def test_no_current_scan_is_rejected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    record: PackageScanRecord,
) -> None:
    """Never, running, and failed scans are not zero updates and never update."""
    await setup_integration(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    manager._set_record("pve1", 200, record)
    with patch.object(manager, "async_start_update") as start:
        await _expect_invalid(
            hass, _device_id(hass, "1234_container_200"), "easy_update_no_current_scan"
        )
    start.assert_not_called()


async def test_empty_scan_is_rejected(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A successful empty plan has nothing to confirm or update."""
    await setup_integration(hass, mock_config_entry)
    empty = PackageScanResult(
        os_id="debian",
        os_version="12",
        packages=(),
        reboot_required=False,
        not_upgraded_count=0,
    )
    _seed_scan(mock_config_entry, result=empty)
    await _expect_invalid(
        hass, _device_id(hass, "1234_container_200"), "easy_update_nothing_to_update"
    )
    assert (
        mock_config_entry.runtime_data.package_manager.record("pve1", 200).reviewed
        is False
    )


async def test_changed_scan_never_authorizes_the_new_plan(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The click authorizes only the scan observation the card displayed."""
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    with patch.object(manager, "async_start_update") as start:
        await _expect_invalid(
            hass,
            _device_id(hass, "1234_container_200"),
            "easy_update_scan_changed",
            expected_scan_attempt="2026-10-02T04:00:00+00:00",
        )
    start.assert_not_called()
    assert manager.record("pve1", 200).reviewed is False


async def test_busy_target_is_rejected_without_leaving_review(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Existing concurrency rules still own conflicts; review is not left set."""
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    manager._health_records["pve1", 200] = PackageHealthRecord(
        check_status=HealthCheckStatus.RUNNING
    )
    with pytest.raises(HomeAssistantError) as err:
        await _easy_update(hass, _device_id(hass, "1234_container_200"))
    assert err.value.translation_key == "package_update_failed"
    assert manager.record("pve1", 200).reviewed is False

    manager._health_records.clear()
    manager._restore_reserved.add(("pve1", 200))
    with pytest.raises(HomeAssistantError):
        await _easy_update(hass, _device_id(hass, "1234_container_200"))
    assert manager.record("pve1", 200).reviewed is False


async def test_manager_rejection_is_reported(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A manager rejection after confirmation surfaces as the existing error."""
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    with (
        patch.object(
            manager,
            "async_start_update",
            side_effect=PackageUpdateError(
                PackageUpdateOutcome.PACKAGE_MANAGER_BUSY, "busy"
            ),
        ),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _easy_update(hass, _device_id(hass, "1234_container_200"))
    assert err.value.translation_placeholders == {"reason": "busy"}


async def test_missing_snapshot_permission_is_rejected(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Update still requires native VM.Snapshot; nothing is confirmed."""
    permissions = deepcopy(mock_proxmox_client.access.permissions.get.return_value)
    for grants in permissions.values():
        grants.pop("VM.Snapshot", None)
    mock_proxmox_client.access.permissions.get.return_value = permissions
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    with pytest.raises(HomeAssistantError):
        await _easy_update(hass, _device_id(hass, "1234_container_200"))
    manager = mock_config_entry.runtime_data.package_manager
    assert manager.record("pve1", 200).reviewed is False
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.NEVER


async def test_device_identity_survives_entity_rename(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    blocked_plan,
) -> None:
    """Renamed entity IDs and device names do not change the resolved target."""
    entered, release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    ent_reg = er.async_get(hass)
    ent_reg.async_update_entity(
        "sensor.ct_nginx_pending_package_updates", new_entity_id="sensor.renamed_x"
    )
    device_id = _device_id(hass, "1234_container_200")
    dr.async_get(hass).async_update_device(device_id, name_by_user="Renamed")
    await _easy_update(hass, device_id)
    manager = mock_config_entry.runtime_data.package_manager
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.RUNNING
    await entered.wait()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("autoremove", [False, True])
async def test_autoremove_choice_controls_only_the_continuation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    blocked_plan,
    autoremove: bool,
) -> None:
    """YOLO off starts no continuation; YOLO on binds to this exact attempt."""
    entered, release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    with patch(
        "custom_components.hubinet_ops.services.async_start_post_update_autoremove"
    ) as follow:
        await _easy_update(
            hass, _device_id(hass, "1234_container_200"), autoremove=autoremove
        )
    manager = mock_config_entry.runtime_data.package_manager
    if autoremove:
        follow.assert_called_once()
        _hass, target, update = follow.call_args.args
        assert (target.node, target.vmid) == ("pve1", 200)
        assert target.entry is mock_config_entry
        assert update.last_attempt == manager.update_record("pve1", 200).last_attempt
    else:
        follow.assert_not_called()
    await entered.wait()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
