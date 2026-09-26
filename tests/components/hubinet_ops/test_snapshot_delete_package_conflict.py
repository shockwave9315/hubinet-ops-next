"""Snapshot Delete and active package mutation truthfulness regressions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.packages.models import (
    CleanupEvidence,
    HealthCheckStatus,
    PackageHealthRecord,
    PackageMutationResult,
    PackageScanRecord,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
)
from custom_components.hubinet_ops.packages.parser import (
    ParsedAptSimulation,
    ParsedAutoremoveSimulation,
)
from custom_components.hubinet_ops.select import ATTR_SELECTED_SNAPSHOT
from custom_components.hubinet_ops.snapshots import (
    RestoreOutcome,
    RestoreResult,
    SnapshotKind,
)
from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.translation import async_get_translations

from . import setup_integration
from .test_package_update import CANDIDATES
from .test_packages import RESULT
from .test_snapshot_delete import (
    DELETE_LXC,
    DELETE_VM,
    KEY,
    NOW,
    SELECT_LXC,
    SELECT_VM,
    _message,
    _prepare,
    _press,
    _select,
)


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> None:
    """Enable the explicit snapshot controls."""


@pytest.mark.parametrize("operation", ["update", "autoremove"])
@pytest.mark.parametrize("mutation_succeeds", [False, True])
async def test_active_package_snapshot_delete_truth(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    operation: str,
    mutation_succeeds: bool,
) -> None:
    """Exercise native creation/deletion and real terminal package reporting."""
    snapshots: set[str] = set()
    statuses: dict[str, dict[str, str]] = {}
    guest = mock_proxmox_client._node_mock.lxc(200)  # noqa: SLF001
    guest.snapshot.get.side_effect = lambda: [
        {"name": name, "snaptime": 1} for name in snapshots
    ]

    def create_native(*, snapname, description):
        snapshots.add(snapname)
        upid = "UPID:pve1:00000001:00000002:00000003:vzsnapshot:200:user@pam:"
        statuses[upid] = {"status": "stopped", "exitstatus": "OK"}
        return upid

    def delete_native():
        name = guest.snapshot.call_args.args[0]
        upid = (
            f"UPID:pve1:{guest.snapshot.return_value.delete.call_count:08X}:"
            "00000002:00000003:vzdelsnapshot:200:user@pam:"
        )
        exitstatus = "OK" if name in snapshots else f"snapshot '{name}' does not exist"
        snapshots.discard(name)
        statuses[upid] = {"status": "stopped", "exitstatus": exitstatus}
        return upid

    guest.snapshot.post.side_effect = create_native
    guest.snapshot.return_value.delete.side_effect = delete_native
    node = mock_proxmox_client._node_mock  # noqa: SLF001
    node.tasks.side_effect = lambda upid: MagicMock(
        status=MagicMock(get=MagicMock(side_effect=lambda: statuses[upid]))
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def mutate(_node, _vmid):
        entered.set()
        await release.wait()
        if not mutation_succeeds:
            raise PackageUpdateError(PackageUpdateOutcome.MUTATION_FAILED, "APT failed")
        return PackageMutationResult({}, {})

    await setup_integration(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    manager._records[KEY] = PackageScanRecord(  # noqa: SLF001
        status=PackageScanStatus.SUCCESS, result=RESULT, token="review", reviewed=True
    )
    manager._cleanup_evidence[KEY] = CleanupEvidence(CANDIDATES, NOW)  # noqa: SLF001
    with (
        patch.object(
            manager._transport,  # noqa: SLF001
            "async_plan",
            AsyncMock(return_value=ParsedAptSimulation(RESULT.packages, 0)),
        ),
        patch.object(
            manager._transport,  # noqa: SLF001
            "async_plan_autoremove",
            AsyncMock(return_value=ParsedAutoremoveSimulation(CANDIDATES, 0)),
        ),
        patch.object(manager._transport, f"async_{operation}", mutate),  # noqa: SLF001
        patch.object(manager._transport, "async_ping", AsyncMock(return_value=True)),  # noqa: SLF001
        patch(
            "custom_components.hubinet_ops.snapshots.Tasks.blocking_status",
            side_effect=lambda _proxmox, upid, **kwargs: statuses[upid],
        ),
    ):
        start = getattr(manager, f"async_start_{operation}")
        record = manager.update_record if operation == "update" else manager.cleanup_record
        task = start(*KEY, target_is_running=True, snapshot_permission=True)
        await entered.wait()
        assert record(*KEY).status is PackageUpdateStatus.RUNNING
        (snapshot_name,) = snapshots
        await async_update_entity(hass, SELECT_LXC)
        await _select(hass, SELECT_LXC, snapshot_name)
        try:
            with pytest.raises(HomeAssistantError, match="Update or Autoremove is running"):
                await _press(hass, DELETE_LXC)
            assert snapshot_name in snapshots
            assert (
                hass.states.get(SELECT_LXC).attributes[ATTR_SELECTED_SNAPSHOT]
                == snapshot_name
            )
            guest.snapshot.return_value.delete.assert_not_called()
        finally:
            release.set()
            await task
            await hass.async_block_till_done()

    terminal = record(*KEY)
    notification_kind = "update" if operation == "update" else "cleanup_result"
    message = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        f"hubinet_ops_package_{notification_kind}_pve1_200"
    ]["message"]
    assert terminal.snapshot_retained == (snapshot_name in snapshots), (
        terminal,
        message,
        snapshots,
    )
    assert terminal.snapshot_cleanup_failed is False
    if mutation_succeeds:
        assert terminal.status is PackageUpdateStatus.SUCCESS
        assert snapshot_name not in snapshots
        assert "Retained safety snapshot" not in message
        guest.snapshot.return_value.delete.assert_called_once_with()
    else:
        assert terminal.status is PackageUpdateStatus.FAILED
        assert f"snapshot was retained as {snapshot_name}" in message
        guest.snapshot.return_value.delete.assert_not_called()


@pytest.mark.parametrize("operation", ["update", "autoremove"])
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_package_conflict_rechecked_after_fresh_listing(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    operation: str,
    language: str,
) -> None:
    """A mutation starting during native validation prevents later submission."""
    name = "hubinet-preupd-before"
    _prepare(mock_proxmox_client, name)
    await setup_integration(hass, mock_config_entry)
    await async_get_translations(hass, language, "exceptions", [DOMAIN])
    hass.config.language = language
    await _select(hass, SELECT_LXC, name)
    manager = mock_config_entry.runtime_data.package_manager
    records = manager._update_records if operation == "update" else manager._cleanup_records  # noqa: SLF001
    running = PackageUpdateRecord(status=PackageUpdateStatus.RUNNING)
    guest = mock_proxmox_client._node_mock.lxc(200)  # noqa: SLF001
    initial_gets = guest.snapshot.get.call_count

    async def validate(*args, **kwargs):
        assert hass.states.get(SELECT_LXC).attributes[ATTR_SELECTED_SNAPSHOT] is None
        records[KEY] = running
        return True

    with (
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_validate_snapshot",
            side_effect=validate,
        ) as fresh,
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot"
        ) as delete,
    ):
        await _press(hass, DELETE_LXC)

    fresh.assert_awaited_once()
    delete.assert_not_called()
    assert records[KEY] is running
    assert not manager.restore_reserved(*KEY)
    assert guest.snapshot.get.call_count == initial_gets  # No success refresh.
    message = _message(hass, mock_config_entry, SnapshotKind.LXC, 200, name)
    assert ("was not started" if language == "en" else "nie zostało rozpoczęte") in message
    assert (
        "Update or Autoremove is running"
        if language == "en"
        else "trwa aktualizacja pakietów lub Autoremove"
    ) in message


@pytest.mark.parametrize("operation", ["update", "autoremove"])
@pytest.mark.parametrize(
    ("kind", "name", "record_vmid", "status"),
    [
        (SnapshotKind.LXC, "manual_before_update", 200, PackageUpdateStatus.RUNNING),
        (SnapshotKind.LXC, "homeassistant_snapshot_before", 200, PackageUpdateStatus.RUNNING),
        (SnapshotKind.QEMU, "hubinet-preupd-before", 100, PackageUpdateStatus.RUNNING),
        (SnapshotKind.LXC, "hubinet-preupd-before", 201, PackageUpdateStatus.RUNNING),
        (SnapshotKind.LXC, "hubinet-preupd-before", 200, PackageUpdateStatus.SUCCESS),
        (SnapshotKind.LXC, "hubinet-preupd-before", 200, PackageUpdateStatus.FAILED),
        (SnapshotKind.LXC, "hubinet-preupd-before", 200, PackageUpdateStatus.NEVER),
    ],
)
async def test_package_conflict_does_not_restrict_other_owner_choices(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    operation: str,
    kind: SnapshotKind,
    name: str,
    record_vmid: int,
    status: PackageUpdateStatus,
) -> None:
    """No generic prefix restriction, QEMU coupling, or Scan/Health exclusion."""
    _prepare(mock_proxmox_client, name)
    await setup_integration(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    records = manager._update_records if operation == "update" else manager._cleanup_records  # noqa: SLF001
    records[("pve1", record_vmid)] = PackageUpdateRecord(status=status)
    manager._records[KEY] = PackageScanRecord(status=PackageScanStatus.RUNNING)  # noqa: SLF001
    manager._health_records[KEY] = PackageHealthRecord(  # noqa: SLF001
        check_status=HealthCheckStatus.RUNNING
    )
    vmid = 100 if kind is SnapshotKind.QEMU else 200
    selector = SELECT_VM if kind is SnapshotKind.QEMU else SELECT_LXC
    button = DELETE_VM if kind is SnapshotKind.QEMU else DELETE_LXC
    await _select(hass, selector, name)
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS)),
    ) as delete:
        await _press(hass, button)
    assert delete.await_args.args[1:5] == ("pve1", vmid, kind, name)
    assert "was deleted" in _message(hass, mock_config_entry, kind, vmid, name)
