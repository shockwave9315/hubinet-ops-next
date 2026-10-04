"""Exact native snapshot Delete operator flow and isolation tests."""

import asyncio
from datetime import UTC, datetime
from html import escape
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.packages.models import (
    CleanupEvidence,
    HealthCheckStatus,
    HealthDpkgState,
    HealthSource,
    HealthState,
    PackageHealthRecord,
    PackageScanRecord,
    PackageScanStatus,
    RemovablePackage,
)
from custom_components.hubinet_ops.select import ATTR_SELECTED_SNAPSHOT
from custom_components.hubinet_ops.snapshot_restore import (
    ProxmoxVMSnapshotDeleteButton,
    _notify_snapshot_delete,
    snapshot_delete_notification_id,
)
from custom_components.hubinet_ops.snapshots import (
    RestoreOutcome,
    RestoreResult,
    SnapshotKind,
)
from homeassistant.components import persistent_notification as pn
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.translation import async_get_translations

from . import setup_integration
from .test_packages import RESULT

KEY = ("pve1", 200)
NOW = datetime(2026, 9, 26, tzinfo=UTC)

UPID = "UPID:pve1:00000001:00000002:00000003:qmdelsnapshot:100:user@pam:"
SELECT_VM = "select.vm_web_snapshot_to_restore"
DELETE_VM = "button.vm_web_delete_snapshot"
SELECT_LXC = "select.ct_nginx_snapshot_to_restore"
DELETE_LXC = "button.ct_nginx_delete_snapshot"


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> None:
    """Enable config-category snapshot controls."""


def _guest(client: MagicMock, family: str, vmid: int) -> MagicMock:
    return getattr(client._node_mock, family)(vmid)  # noqa: SLF001


def _prepare(client: MagicMock, name: str = "manual_before_update") -> None:
    for family, vmid in (("qemu", 100), ("lxc", 200), ("lxc", 201)):
        _guest(client, family, vmid).snapshot.get.return_value = [
            {"name": name, "snaptime": 1},
            {"name": "newest", "snaptime": 2},
        ]


async def _select(hass: HomeAssistant, entity: str, name: str) -> None:
    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity, ATTR_OPTION: name},
        blocking=True,
    )


async def _press(hass: HomeAssistant, entity: str) -> None:
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: entity}, blocking=True
    )
    await hass.async_block_till_done()


def _message(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    kind: SnapshotKind,
    vmid: int,
    name: str,
) -> str:
    return pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_delete_notification_id(entry.entry_id, kind, "pve1", vmid, name)
    ]["message"]


async def test_delete_without_selection_rejected(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """No default/newest snapshot can become deletion authority."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    with (
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot"
        ) as delete,
        pytest.raises(HomeAssistantError, match="Select one exact native snapshot"),
    ):
        await _press(hass, DELETE_VM)
    delete.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "vmid", "selector", "button"),
    [
        (SnapshotKind.QEMU, 100, SELECT_VM, DELETE_VM),
        (SnapshotKind.LXC, 200, SELECT_LXC, DELETE_LXC),
    ],
)
@pytest.mark.parametrize("name", ["manual_before_update", "unknown", "unavailable"])
async def test_delete_exact_choice_fresh_validation_and_targeted_success(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    kind: SnapshotKind,
    vmid: int,
    selector: str,
    button: str,
    name: str,
) -> None:
    """Manual snapshots and HA sentinel names use exact identity, one DELETE, and scoped refresh."""
    _prepare(mock_proxmox_client, name)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, selector, name)
    guest = _guest(mock_proxmox_client, kind.value, vmid)
    initial_gets = guest.snapshot.get.call_count
    others = [
        _guest(mock_proxmox_client, family, other).snapshot.get
        for family, other in (("qemu", 100), ("lxc", 200), ("lxc", 201))
        if (family, other) != (kind.value, vmid)
    ]
    other_calls = [get.call_count for get in others]
    coordinator = mock_config_entry.runtime_data
    coordinator.async_request_refresh = AsyncMock()

    def delete_native():
        # Acceptance has consumed the selector before fresh listing and submission.
        assert hass.states.get(selector).attributes[ATTR_SELECTED_SNAPSHOT] is None
        assert guest.snapshot.get.call_count == initial_gets + 1
        guest.snapshot.get.return_value = [{"name": "newest", "snaptime": 2}]
        return UPID

    guest.snapshot.return_value.delete.side_effect = delete_native
    with patch(
        "custom_components.hubinet_ops.snapshots.async_observe_task",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS, UPID)),
    ) as observe:
        await _press(hass, button)

    guest.snapshot.assert_called_once_with(name)
    guest.snapshot.return_value.delete.assert_called_once_with()
    observe.assert_awaited_once_with(
        mock_proxmox_client, "pve1", UPID, executor=hass.async_add_executor_job
    )
    state = hass.states.get(selector)
    assert state.attributes["options"] == ["newest"]
    assert state.attributes[ATTR_SELECTED_SNAPSHOT] is None
    assert guest.snapshot.get.call_count == initial_gets + 2
    assert [get.call_count for get in others] == other_calls
    coordinator.async_request_refresh.assert_not_awaited()
    message = _message(hass, mock_config_entry, kind, vmid, name)
    assert "was deleted" in message
    assert name in message
    assert f"pve1/{vmid}" in message
    assert "does not appear to have been created by Hubinet-Ops" in message


@pytest.mark.parametrize(
    "rows", [[], [{"name": "manual_before_update", "snapstate": "delete"}]]
)
async def test_delete_vanished_or_ineligible_not_started(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    rows: object,
) -> None:
    """Presentation is freshly revalidated and cannot authorize a stale target."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC, "manual_before_update")
    guest = _guest(mock_proxmox_client, "lxc", 200)
    guest.snapshot.get.return_value = rows
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot"
    ) as delete:
        await _press(hass, DELETE_LXC)
    delete.assert_not_called()
    assert "was not started" in _message(
        hass, mock_config_entry, SnapshotKind.LXC, 200, "manual_before_update"
    )
    assert hass.states.get(SELECT_LXC).attributes[ATTR_SELECTED_SNAPSHOT] is None


@pytest.mark.parametrize("outcome", list(RestoreOutcome))
async def test_delete_preserves_all_package_truth_and_refreshes_only_success(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    outcome: RestoreOutcome,
) -> None:
    """No terminal deletion outcome reserves Restore or invalidates package/Health evidence."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC, "manual_before_update")
    coordinator = mock_config_entry.runtime_data
    manager = coordinator.package_manager
    scan = PackageScanRecord(
        status=PackageScanStatus.SUCCESS, result=RESULT, token="token", reviewed=True
    )
    evidence = CleanupEvidence((RemovablePackage("unused", "amd64", "1"),), NOW)
    manager._records[KEY] = scan  # noqa: SLF001
    manager._cleanup_evidence[KEY] = evidence  # noqa: SLF001
    manager._viewed_tokens[KEY] = "token"  # noqa: SLF001
    manager._health_records[KEY] = PackageHealthRecord(  # noqa: SLF001
        check_status=HealthCheckStatus.COMPLETED,
        state=HealthState.HEALTHY,
        source=HealthSource.MANUAL,
        checked_at=NOW,
        guest_exec=True,
        dpkg=HealthDpkgState.OK,
    )
    before = (
        manager.update_record(*KEY),
        manager.cleanup_record(*KEY),
        manager.health_record(*KEY),
    )
    coordinator.async_request_refresh = AsyncMock()
    guest = _guest(mock_proxmox_client, "lxc", 200)
    calls = guest.snapshot.get.call_count
    with (
        patch.object(
            manager,
            "begin_restore",
            side_effect=AssertionError("Delete cannot reserve Restore"),
        ),
        patch.object(
            manager,
            "invalidate_restore_target",
            side_effect=AssertionError("Delete cannot invalidate"),
        ),
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot",
            AsyncMock(return_value=RestoreResult(outcome, UPID, "bounded <error>")),
        ),
    ):
        await _press(hass, DELETE_LXC)
    assert manager.record(*KEY) is scan
    assert manager.cleanup_evidence(*KEY) is evidence
    assert manager.viewed_token(*KEY) == "token"
    assert (
        manager.update_record(*KEY),
        manager.cleanup_record(*KEY),
        manager.health_record(*KEY),
    ) == before
    assert not manager.restore_reserved(*KEY)
    assert guest.snapshot.get.call_count == calls + 1 + (
        outcome is RestoreOutcome.SUCCESS
    )
    coordinator.async_request_refresh.assert_not_awaited()
    message = _message(
        hass, mock_config_entry, SnapshotKind.LXC, 200, "manual_before_update"
    )
    assert "bounded &lt;error&gt;" in message
    assert {
        RestoreOutcome.SUCCESS: "was deleted",
        RestoreOutcome.FAILED: "PVE reported an error",
        RestoreOutcome.NOT_STARTED: "was not started",
        RestoreOutcome.UNCERTAIN: "could not be established",
    }[outcome] in message
    assert "manual Scan" not in message
    assert UPID in message


@pytest.mark.parametrize(
    "name", ["homeassistant_snapshot_20260926120000", "hubinet-preupd-20260926"]
)
async def test_delete_hubinet_names_no_external_warning(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    name: str,
) -> None:
    """Recognizable names affect only the optional warning copy."""
    _prepare(mock_proxmox_client, name)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM, name)
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS)),
    ) as delete:
        await _press(hass, DELETE_VM)
    assert delete.await_args.args[4] == name
    assert "does not appear" not in _message(
        hass, mock_config_entry, SnapshotKind.QEMU, 100, name
    )


async def test_delete_tracked_background_does_not_hold_button_and_unload_cancels(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Config-entry unload owns long observation and cancellation stays uncertain."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM, "manual_before_update")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return RestoreResult(RestoreOutcome.SUCCESS)

    with (
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot",
            side_effect=blocked,
        ),
        patch.object(
            mock_config_entry,
            "async_create_background_task",
            wraps=mock_config_entry.async_create_background_task,
        ) as tracked,
    ):
        await hass.services.async_call(
            "button", SERVICE_PRESS, {ATTR_ENTITY_ID: DELETE_VM}, blocking=True
        )
        await asyncio.wait_for(entered.wait(), 1)
        tracked.assert_called_once()
        assert tracked.call_args.args[2] == "snapshot Delete pve1/100"
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.pve1_restart"},
            blocking=True,
        )
        mock_proxmox_client._node_mock.status.post.assert_called_with(command="reboot")  # noqa: SLF001
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert "could not be established" in _message(
        hass, mock_config_entry, SnapshotKind.QEMU, 100, "manual_before_update"
    )


@pytest.mark.parametrize("permission", ["VM.Audit", "VM.Snapshot", "VM.PowerMgmt"])
async def test_delete_requires_existing_selector_boundary(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    permission: str,
) -> None:
    """No Delete control exists without the already-existing selector boundary."""
    _prepare(mock_proxmox_client)
    permissions = mock_proxmox_client.access.permissions.get.return_value.copy()
    permissions["/vms/100"] = {permission: 0}
    mock_proxmox_client.access.permissions.get.return_value = permissions
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get(SELECT_VM) is None
    assert hass.states.get(DELETE_VM) is None


async def test_delete_rechecks_revoked_permission_at_press(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Stale UI availability never skips the acceptance permission check."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM, "manual_before_update")
    coordinator = mock_config_entry.runtime_data
    coordinator.permissions = {
        path: dict(values) for path, values in coordinator.permissions.items()
    }
    coordinator.permissions["/vms/100"] = {"VM.Snapshot": 0}
    node = coordinator.data["pve1"]
    button = ProxmoxVMSnapshotDeleteButton(coordinator, node.vms[100], node)
    button.hass = hass
    with pytest.raises(HomeAssistantError, match="existing selector permissions"):
        await button.async_press()
    assert (
        hass.states.get(SELECT_VM).attributes[ATTR_SELECTED_SNAPSHOT]
        == "manual_before_update"
    )


async def test_delete_polish_result_and_warning(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Shipped Polish UX includes exact target and warning-only copy."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await async_get_translations(hass, "pl", "exceptions", [DOMAIN])
    hass.config.language = "pl"
    await _select(hass, SELECT_LXC, "manual_before_update")
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS)),
    ):
        await _press(hass, DELETE_LXC)
    message = _message(
        hass, mock_config_entry, SnapshotKind.LXC, 200, "manual_before_update"
    )
    assert "został usunięty" in message
    assert "nie blokuje usuwania" in message


async def test_delete_warning_escapes_exact_target(hass: HomeAssistant) -> None:
    """Foreign display text is escaped in the warning and result."""
    await async_get_translations(hass, "en", "exceptions", [DOMAIN])
    node = "pve<node>"
    name = "external_<snapshot>"
    _notify_snapshot_delete(
        hass,
        "entry",
        SnapshotKind.LXC,
        node,
        200,
        name,
        RestoreResult(RestoreOutcome.NOT_STARTED),
    )
    message = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_delete_notification_id("entry", SnapshotKind.LXC, node, 200, name)
    ]["message"]
    assert message.count(escape(node)) == 2
    assert message.count(escape(name)) == 2
    assert node not in message
    assert name not in message


async def test_delete_fresh_listing_failure_not_started(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An unavailable fresh listing cannot authorize DELETE or trigger refresh."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM, "manual_before_update")
    guest = _guest(mock_proxmox_client, "qemu", 100)
    calls = guest.snapshot.get.call_count
    guest.snapshot.get.side_effect = ConnectionError("unavailable")
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot"
    ) as delete:
        await _press(hass, DELETE_VM)
    delete.assert_not_called()
    assert guest.snapshot.get.call_count == calls + 1
    assert "was not started" in _message(
        hass, mock_config_entry, SnapshotKind.QEMU, 100, "manual_before_update"
    )


async def test_delete_task_creation_failure_closes_unstarted_coroutine(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A task creation failure cannot leak execution beyond config-entry ownership."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM, "manual_before_update")
    with (
        patch.object(
            mock_config_entry,
            "async_create_background_task",
            side_effect=RuntimeError("task creation failed"),
        ) as tracked,
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot"
        ) as delete,
        pytest.raises(RuntimeError, match="task creation failed"),
    ):
        await _press(hass, DELETE_VM)
    assert tracked.call_args.args[1].cr_frame is None
    delete.assert_not_called()


async def test_delete_missing_guest_rejected_before_consuming_selection(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Acceptance checks current guest existence independently of stale UI state."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM, "manual_before_update")
    coordinator = mock_config_entry.runtime_data
    node = coordinator.data["pve1"]
    button = ProxmoxVMSnapshotDeleteButton(coordinator, node.vms[100], node)
    button.hass = hass
    node.vms.pop(100)
    with pytest.raises(HomeAssistantError, match="guest is no longer present"):
        await button.async_press()
    assert (
        hass.states.get(SELECT_VM).attributes[ATTR_SELECTED_SNAPSHOT]
        == "manual_before_update"
    )


@pytest.mark.parametrize("during_validation", [False, True])
@pytest.mark.parametrize(
    ("kind", "vmid", "selector", "button", "create"),
    [
        (SnapshotKind.QEMU, 100, SELECT_VM, DELETE_VM, "button.vm_web_create_snapshot"),
        (
            SnapshotKind.LXC,
            200,
            SELECT_LXC,
            DELETE_LXC,
            "button.ct_nginx_create_snapshot",
        ),
    ],
)
async def test_delete_blocks_same_guest_create_before_acceptance_and_mutation(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    during_validation: bool,
    kind: SnapshotKind,
    vmid: int,
    selector: str,
    button: str,
    create: str,
) -> None:
    """Create starting during fresh validation still prevents native deletion."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, selector, "manual_before_update")
    create_entered = asyncio.Event()
    create_release = asyncio.Event()
    validation_entered = asyncio.Event()
    validation_release = asyncio.Event()

    async def observe(*_args, **_kwargs) -> RestoreResult:
        create_entered.set()
        await create_release.wait()
        return RestoreResult(RestoreOutcome.UNCERTAIN)

    async def validate(*_args, **_kwargs) -> bool:
        validation_entered.set()
        await validation_release.wait()
        return True

    with (
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
            side_effect=observe,
        ),
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_validate_snapshot",
            side_effect=validate,
        ) as validation,
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot"
        ) as delete,
    ):
        if during_validation:
            await hass.services.async_call(
                "button", SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
            )
            await asyncio.wait_for(validation_entered.wait(), 1)
        await hass.services.async_call(
            "button", SERVICE_PRESS, {ATTR_ENTITY_ID: create}, blocking=True
        )
        await asyncio.wait_for(create_entered.wait(), 1)
        assert hass.states.get(create).attributes["snapshot_create_running"] is True
        if during_validation:
            validation_release.set()
            await hass.async_block_till_done()
            message = _message(
                hass, mock_config_entry, kind, vmid, "manual_before_update"
            )
            assert "still being observed" in message
            assert "completed successfully" not in message
        else:
            with pytest.raises(HomeAssistantError, match="still being observed"):
                await hass.services.async_call(
                    "button", SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
                )
            assert (
                hass.states.get(selector).attributes[ATTR_SELECTED_SNAPSHOT]
                == "manual_before_update"
            )
            validation.assert_not_called()
        delete.assert_not_called()
        create_release.set()
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    "create", ["button.vm_web_create_snapshot", "button.ct_backup_create_snapshot"]
)
async def test_delete_other_guest_remains_available_during_create(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    create: str,
) -> None:
    """An LXC Delete remains independent of another guest's Create task."""
    _prepare(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC, "manual_before_update")
    release = asyncio.Event()

    async def observe(*_args, **_kwargs) -> RestoreResult:
        await release.wait()
        return RestoreResult(RestoreOutcome.UNCERTAIN)

    with (
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
            side_effect=observe,
        ),
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_delete_snapshot",
            AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS)),
        ) as delete,
    ):
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: create},
            blocking=True,
        )
        await _press(hass, DELETE_LXC)
        delete.assert_awaited_once()
        assert hass.states.get(create).attributes["snapshot_create_running"] is True
        release.set()
        await hass.async_block_till_done()
