"""Exercise the device-targeted Easy Update action over the real manager."""

# ruff: noqa: SLF001 -- seed and inspect existing ephemeral package records

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from tests.common import MockConfigEntry, MockUser  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.packages.models import (
    HealthCheckStatus,
    PackageHealthRecord,
    PackageScanRecord,
    PackageScanResult,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
)
from custom_components.hubinet_ops.snapshots import RestoreOutcome, RestoreResult
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
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
    assert manager.update_record("pve1", 200).snapshot_skipped is False
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


@pytest.mark.parametrize("skip_snapshot", [False, True])
async def test_changed_scan_never_authorizes_the_new_plan(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    skip_snapshot: bool,
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
            skip_snapshot=skip_snapshot,
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


@pytest.mark.parametrize("skip_snapshot", [False, True])
async def test_stale_coordinator_data_is_rejected_without_side_effects(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    skip_snapshot: bool,
) -> None:
    """A failed latest Proxmox refresh never starts Easy Update."""
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    coordinator.last_update_success = False
    await _expect_invalid(
        hass,
        _device_id(hass, "1234_container_200"),
        "easy_update_not_running",
        skip_snapshot=skip_snapshot,
    )
    manager = coordinator.package_manager
    record = manager.record("pve1", 200)
    assert record.status is PackageScanStatus.SUCCESS
    assert record.token == TOKEN
    assert record.reviewed is False
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.NEVER


UPDATE_BUTTON = "button.ct_nginx_update_packages"


async def _expect_unauthorized(hass: HomeAssistant, user_id: str, **data) -> None:
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "easy_update",
            {"device_id": _device_id(hass, "1234_container_200"), **data},
            blocking=True,
            context=Context(user_id=user_id),
        )


@pytest.mark.parametrize("skip_snapshot", [False, True])
async def test_user_without_control_is_unauthorized_before_review(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    hass_read_only_user: MockUser,
    skip_snapshot: bool,
) -> None:
    """A restricted user needs CONTROL on the Update button; nothing changes."""
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    await _expect_unauthorized(
        hass, hass_read_only_user.id, skip_snapshot=skip_snapshot
    )
    manager = mock_config_entry.runtime_data.package_manager
    assert manager.record("pve1", 200).reviewed is False
    assert manager.record("pve1", 200).token == TOKEN
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.NEVER


async def test_autoremove_also_requires_control_on_autoremove(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    blocked_plan,
) -> None:
    """CONTROL on Update alone allows Update, but not Update plus Autoremove."""
    entered, release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    user = MockUser().add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": {UPDATE_BUTTON: True}}})
    manager = mock_config_entry.runtime_data.package_manager

    await _expect_unauthorized(hass, user.id, autoremove=True)
    assert manager.record("pve1", 200).reviewed is False
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.NEVER

    await hass.services.async_call(
        DOMAIN,
        "easy_update",
        {"device_id": _device_id(hass, "1234_container_200")},
        blocking=True,
        context=Context(user_id=user.id),
    )
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.RUNNING
    await entered.wait()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_admin_user_is_unchanged(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    hass_admin_user: MockUser,
    blocked_plan,
) -> None:
    """Administrators need no per-entity policy, as before."""
    entered, release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    await hass.services.async_call(
        DOMAIN,
        "easy_update",
        {"device_id": _device_id(hass, "1234_container_200"), "autoremove": True},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    manager = mock_config_entry.runtime_data.package_manager
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.RUNNING
    await entered.wait()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_skip_without_snapshot_permission_still_requires_and_accepts_control(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    blocked_plan,
    hass_read_only_user: MockUser,
) -> None:
    """The unavailable native button remains a real CONTROL authorization target."""
    permissions = deepcopy(mock_proxmox_client.access.permissions.get.return_value)
    for grants in permissions.values():
        grants.pop("VM.Snapshot", None)
    mock_proxmox_client.access.permissions.get.return_value = permissions
    entered, release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    button = hass.states.get(UPDATE_BUTTON)
    assert button is not None
    assert button.state == "unavailable"
    assert button.attributes["snapshot_permission"] is False
    await _expect_unauthorized(hass, hass_read_only_user.id, skip_snapshot=True)
    assert manager.record("pve1", 200).reviewed is False
    # Same permission boundary as native button press, despite VM.Snapshot absence.
    user = MockUser().add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": {UPDATE_BUTTON: True}}})
    with patch.object(
        manager, "async_start_update", wraps=manager.async_start_update
    ) as start:
        await hass.services.async_call(
            DOMAIN,
            "easy_update",
            {
                "device_id": _device_id(hass, "1234_container_200"),
                "skip_snapshot": True,
            },
            blocking=True,
            context=Context(user_id=user.id),
        )
    start.assert_called_once_with(
        "pve1",
        200,
        target_is_running=True,
        snapshot_permission=False,
        skip_snapshot=True,
    )
    assert manager.record("pve1", 200).status is PackageScanStatus.NEVER
    assert manager.update_record("pve1", 200).snapshot_skipped is True
    await entered.wait()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (
        manager.update_record("pve1", 200).outcome is PackageUpdateOutcome.PLAN_FAILED
    )
    assert manager.update_record("pve1", 200).snapshot_skipped is True


@pytest.mark.parametrize(
    ("guard", "key"),
    [
        ("stopped", "easy_update_not_running"),
        ("never", "easy_update_no_current_scan"),
        ("scan", "easy_update_no_current_scan"),
        ("failed", "easy_update_no_current_scan"),
        ("no_result", "easy_update_no_current_scan"),
        ("no_token", "easy_update_no_current_scan"),
        ("empty", "easy_update_nothing_to_update"),
        ("restore", "package_update_failed"),
        ("update", "package_update_failed"),
        ("autoremove", "package_update_failed"),
        ("health", "package_update_failed"),
    ],
)
async def test_skip_keeps_easy_update_evidence_and_busy_guards_before_review(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    guard: str,
    key: str,
) -> None:
    """Skip cannot confirm an invalid plan or begin work on a stopped/busy LXC."""
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    manager = coordinator.package_manager
    record = manager.record("pve1", 200)
    if guard == "stopped":
        coordinator.data["pve1"].containers[200]["status"] = "stopped"
    elif guard in {"never", "scan", "failed", "no_result", "no_token", "empty"}:
        changes = {
            "never": {"status": PackageScanStatus.NEVER},
            "scan": {"status": PackageScanStatus.RUNNING},
            "failed": {"status": PackageScanStatus.FAILED},
            "no_result": {"result": None},
            "no_token": {"token": None},
            "empty": {"result": replace(RESULT, packages=())},
        }
        manager._set_record("pve1", 200, replace(record, **changes[guard]))
    elif guard == "restore":
        manager._restore_reserved.add(("pve1", 200))
    elif guard == "update":
        manager._update_records["pve1", 200] = PackageUpdateRecord(
            status=PackageUpdateStatus.RUNNING
        )
    elif guard == "autoremove":
        manager._cleanup_records["pve1", 200] = PackageUpdateRecord(
            status=PackageUpdateStatus.RUNNING
        )
    else:
        manager._health_records["pve1", 200] = PackageHealthRecord(
            check_status=HealthCheckStatus.RUNNING
        )
    original = manager.record("pve1", 200)
    with patch.object(
        manager, "confirm_review", wraps=manager.confirm_review
    ) as confirm:
        with pytest.raises(HomeAssistantError) as err:
            await _easy_update(
                hass, _device_id(hass, "1234_container_200"), skip_snapshot=True
            )
        assert err.value.translation_key == key
    confirm.assert_not_called()
    assert manager.record("pve1", 200) is original
    assert original.reviewed is False


async def test_skip_does_not_resolve_invalid_targets(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Skip neither broadens the resolver nor enables unloaded package transport."""
    await setup_integration(hass, mock_config_entry)
    for identifier in ("1234_vm_100", "1234_node_node/pve1"):
        await _expect_invalid(
            hass,
            _device_id(hass, identifier),
            "easy_update_not_package_lxc",
            skip_snapshot=True,
        )
    await _expect_invalid(
        hass, "missing-device", "easy_update_unknown_device", skip_snapshot=True
    )
    device_id = _device_id(hass, "1234_container_200")
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await _expect_invalid(
        hass, device_id, "easy_update_not_package_lxc", skip_snapshot=True
    )


CREATE_UPID = "UPID:pve1:00000001:00000002:00000003:vzsnapshot:200:user@pam:"


@pytest.fixture
def blocked_create():
    """Hold every native snapshot Create observation open until released."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def observe(_proxmox, _node, upid, **_kwargs) -> RestoreResult:
        entered.set()
        await release.wait()
        return RestoreResult(RestoreOutcome.SUCCESS, upid)

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        side_effect=observe,
    ):
        yield entered, release


async def _press_create(
    hass: HomeAssistant, client: MagicMock, family: str, vmid: int, button: str
) -> MagicMock:
    """Start a native Create for one guest and return its POST mock."""
    post = getattr(client._node_mock, family)(vmid).snapshot.post
    post.return_value = CREATE_UPID
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    assert hass.states.get(button).attributes["snapshot_create_running"] is True
    return post


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_skip_is_rejected_while_the_same_guest_create_runs(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    blocked_create,
    blocked_plan,
) -> None:
    """Nothing is confirmed, consumed or started; afterwards the same skip runs."""
    create_entered, create_release = blocked_create
    plan_entered, plan_release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    original = manager.record("pve1", 200)
    device_id = _device_id(hass, "1234_container_200")
    post = await _press_create(
        hass, mock_proxmox_client, "lxc", 200, "button.ct_nginx_create_snapshot"
    )
    await asyncio.wait_for(create_entered.wait(), 1)
    snapshot = mock_proxmox_client._lxc_mocks[200].snapshot
    native_calls = list(snapshot.mock_calls)

    with (
        patch.object(
            manager, "confirm_review", wraps=manager.confirm_review
        ) as confirm,
        patch.object(
            manager, "async_start_update", wraps=manager.async_start_update
        ) as start,
    ):
        for _attempt in range(2):
            with pytest.raises(HomeAssistantError) as err:
                await _easy_update(
                    hass,
                    device_id,
                    skip_snapshot=True,
                    expected_scan_attempt=SCAN_AT.isoformat(),
                )
            assert err.value.translation_key == "snapshot_create_running"
        confirm.assert_not_called()
        start.assert_not_called()

    # The review and its token are untouched, and no Update exists.
    assert manager.record("pve1", 200) is original
    assert original.status is PackageScanStatus.SUCCESS
    assert original.token == TOKEN
    assert original.reviewed is False
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.NEVER
    await hass.async_block_till_done()
    assert not plan_entered.is_set()
    # No native snapshot listing, creation or deletion beyond the manual Create.
    post.assert_called_once()
    assert snapshot.mock_calls == native_calls

    # Once Create has finished, the same valid request is accepted.
    create_release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (
        hass.states.get("button.ct_nginx_create_snapshot").attributes[
            "snapshot_create_running"
        ]
        is False
    )
    assert manager.record("pve1", 200) is original
    await _easy_update(
        hass,
        device_id,
        skip_snapshot=True,
        expected_scan_attempt=SCAN_AT.isoformat(),
    )
    update = manager.update_record("pve1", 200)
    assert update.status is PackageUpdateStatus.RUNNING
    assert update.snapshot_skipped is True
    await plan_entered.wait()
    plan_release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    post.assert_called_once()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("family", "vmid", "button"),
    [
        pytest.param("lxc", 201, "button.ct_backup_create_snapshot", id="other-lxc"),
        pytest.param("qemu", 100, "button.vm_web_create_snapshot", id="vm"),
    ],
)
async def test_create_of_another_guest_does_not_block_skip(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    blocked_create,
    blocked_plan,
    family: str,
    vmid: int,
    button: str,
) -> None:
    """The guard concerns only the Create of the guest being updated."""
    create_entered, create_release = blocked_create
    plan_entered, plan_release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    await _press_create(hass, mock_proxmox_client, family, vmid, button)
    await asyncio.wait_for(create_entered.wait(), 1)

    await _easy_update(
        hass,
        _device_id(hass, "1234_container_200"),
        skip_snapshot=True,
        expected_scan_attempt=SCAN_AT.isoformat(),
    )
    update = manager.update_record("pve1", 200)
    assert update.status is PackageUpdateStatus.RUNNING
    assert update.snapshot_skipped is True
    assert hass.states.get(button).attributes["snapshot_create_running"] is True
    await plan_entered.wait()
    plan_release.set()
    create_release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_create_guard_applies_to_skip_only(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    blocked_create,
    blocked_plan,
) -> None:
    """The normal snapshot-required Update keeps its existing acceptance."""
    create_entered, create_release = blocked_create
    plan_entered, plan_release = blocked_plan
    await setup_integration(hass, mock_config_entry)
    _seed_scan(mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    await _press_create(
        hass, mock_proxmox_client, "lxc", 200, "button.ct_nginx_create_snapshot"
    )
    await asyncio.wait_for(create_entered.wait(), 1)

    await _easy_update(
        hass,
        _device_id(hass, "1234_container_200"),
        expected_scan_attempt=SCAN_AT.isoformat(),
    )
    update = manager.update_record("pve1", 200)
    assert update.status is PackageUpdateStatus.RUNNING
    assert update.snapshot_skipped is False
    await plan_entered.wait()
    plan_release.set()
    create_release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
