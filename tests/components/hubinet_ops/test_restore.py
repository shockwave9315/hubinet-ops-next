"""Tests for explicit native snapshot Restore UX and outcome handling."""

import asyncio
from datetime import UTC, datetime
from threading import Event as ThreadEvent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.packages.models import (
    CleanupEvidence,
    PackageScanRecord,
    PackageScanStatus,
    RemovablePackage,
)
from custom_components.hubinet_ops.select import ATTR_SELECTED_SNAPSHOT
from custom_components.hubinet_ops.snapshot_restore import (
    snapshot_restore_notification_id,
)
from custom_components.hubinet_ops.snapshots import (
    RestoreOutcome,
    RestoreResult,
    SnapshotKind,
)
from homeassistant.components import persistent_notification as pn
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.translation import async_get_translations

from . import setup_integration
from .test_packages import RESULT

SELECT_VM = "select.vm_web_snapshot_to_restore"
RESTORE_VM = "button.vm_web_restore"
SELECT_LXC = "select.ct_nginx_snapshot_to_restore"
RESTORE_LXC = "button.ct_nginx_restore"
SCAN_LXC = "button.ct_nginx_scan_pending_packages"
UPDATE_LXC = "button.ct_nginx_update_packages"
AUTOREMOVE_LXC = "button.ct_nginx_autoremove_unused_packages"
KEY = ("pve1", 200)
NOW = datetime(2026, 9, 11, tzinfo=UTC)


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> None:
    """Enable config-category Restore entities."""


def _set_snapshot_rows(
    mock_proxmox_client: MagicMock, family: str, vmid: int, rows: object
) -> MagicMock:
    guest = getattr(mock_proxmox_client._node_mock, family)(vmid)  # noqa: SLF001
    guest.snapshot.get.return_value = rows
    return guest.snapshot.get


def _prepare_snapshot_rows(mock_proxmox_client: MagicMock) -> None:
    for family, vmid in (("qemu", 100), ("lxc", 200), ("lxc", 201)):
        _set_snapshot_rows(
            mock_proxmox_client,
            family,
            vmid,
            [{"name": "wanted", "snaptime": 1}],
        )


async def _select(hass: HomeAssistant, entity_id: str, name: str = "wanted") -> None:
    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: name},
        blocking=True,
    )


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def test_press_without_explicit_selection_is_rejected(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A Restore button alone has no execution authority."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    with pytest.raises(HomeAssistantError):
        await _press(hass, RESTORE_VM)


async def test_qemu_restore_consumes_choice_without_package_coupling(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """QEMU captures the explicit attribute and launches native Restore only."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM)
    manager = mock_config_entry.runtime_data.package_manager
    manager.begin_restore = MagicMock(side_effect=AssertionError("LXC-only"))
    manager.invalidate_restore_target = MagicMock(
        side_effect=AssertionError("LXC-only")
    )
    result = RestoreResult(RestoreOutcome.SUCCESS, "UPID:test")
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        AsyncMock(return_value=result),
    ) as rollback:
        await _press(hass, RESTORE_VM)

    rollback.assert_awaited_once()
    state = hass.states.get(SELECT_VM)
    assert state is not None
    assert state.attributes[ATTR_SELECTED_SNAPSHOT] is None
    manager.begin_restore.assert_not_called()
    manager.invalidate_restore_target.assert_not_called()


async def test_restore_background_work_does_not_hold_native_button_semaphore(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A long rollback task does not serialize unrelated native button calls."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_rollback(*_args, **_kwargs) -> RestoreResult:
        entered.set()
        await release.wait()
        return RestoreResult(RestoreOutcome.SUCCESS)

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        side_effect=blocked_rollback,
    ):
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: RESTORE_VM},
            blocking=True,
        )
        await asyncio.wait_for(entered.wait(), 1)
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.pve1_restart"},
            blocking=True,
        )
        mock_proxmox_client._node_mock.status.post.assert_called_with(  # noqa: SLF001
            command="reboot"
        )
        release.set()
        await hass.async_block_till_done()


@pytest.mark.parametrize("snapshot_name", ["unknown", "unavailable"])
async def test_restore_uses_explicit_attribute_for_sentinel_names(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    snapshot_name: str,
) -> None:
    """Never use HA's sentinel-colliding State.state as target identity."""
    _prepare_snapshot_rows(mock_proxmox_client)
    _set_snapshot_rows(
        mock_proxmox_client,
        "qemu",
        100,
        [{"name": snapshot_name, "snaptime": 1}],
    )
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_VM, snapshot_name)
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS)),
    ) as rollback:
        await _press(hass, RESTORE_VM)
    assert rollback.await_args.args[4] == snapshot_name


async def test_stale_or_active_snapshot_is_not_started_before_lxc_invalidation(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Fresh GET rejects a vanished/transient exact target without package mutation."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC)
    _set_snapshot_rows(
        mock_proxmox_client,
        "lxc",
        200,
        [{"name": "wanted", "snaptime": 1, "snapstate": "rollback"}],
    )
    manager = mock_config_entry.runtime_data.package_manager
    manager.invalidate_restore_target = MagicMock(
        wraps=manager.invalidate_restore_target
    )
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        AsyncMock(),
    ) as rollback:
        await _press(hass, RESTORE_LXC)

    rollback.assert_not_awaited()
    manager.invalidate_restore_target.assert_not_called()
    assert not manager.restore_reserved(*KEY)


@pytest.mark.parametrize(
    ("outcome", "reserved"),
    [
        (RestoreOutcome.NOT_STARTED, False),
        (RestoreOutcome.SUCCESS, False),
        (RestoreOutcome.FAILED, False),
        (RestoreOutcome.UNCERTAIN, True),
    ],
)
async def test_lxc_restore_outcome_release_table(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    outcome: RestoreOutcome,
    reserved: bool,
) -> None:
    """Release only known terminal outcomes after package truth invalidation."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    manager._records[KEY] = PackageScanRecord(  # noqa: SLF001
        status=PackageScanStatus.SUCCESS,
        result=RESULT,
        token="token",
        reviewed=True,
    )
    manager._viewed_tokens[KEY] = "token"  # noqa: SLF001
    manager._cleanup_evidence[KEY] = CleanupEvidence(  # noqa: SLF001
        (RemovablePackage("unused", "amd64", "1"),),
        NOW,
    )
    await _select(hass, SELECT_LXC)
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        AsyncMock(return_value=RestoreResult(outcome, "UPID:test", "reason")),
    ):
        await _press(hass, RESTORE_LXC)

    assert manager.restore_reserved(*KEY) is reserved
    assert manager.record(*KEY) == PackageScanRecord()
    assert manager.cleanup_evidence(*KEY) is None
    assert KEY in manager._fenced_evidence  # noqa: SLF001
    assert hass.states.get("sensor.ct_nginx_pending_package_updates").state == "unknown"
    assert hass.states.get("sensor.ct_nginx_unused_packages").state == "unknown"


async def test_cancellation_after_possible_submission_keeps_lxc_reservation(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """No unconditional finally-release can reopen package controls."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC)
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        AsyncMock(side_effect=asyncio.CancelledError),
    ):
        await _press(hass, RESTORE_LXC)
    assert mock_config_entry.runtime_data.package_manager.restore_reserved(*KEY)
    assert hass.states.get(RESTORE_LXC).state == STATE_UNAVAILABLE


async def test_background_task_creation_failure_releases_lxc_reservation(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A failure before background execution cannot strand operation ownership."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC)
    manager = mock_config_entry.runtime_data.package_manager
    with (
        patch.object(
            mock_config_entry,
            "async_create_background_task",
            side_effect=RuntimeError("task creation failed"),
        ),
        pytest.raises(RuntimeError),
    ):
        await _press(hass, RESTORE_LXC)
    assert not manager.restore_reserved(*KEY)


@pytest.mark.parametrize("failure", [RuntimeError("validation failed"), asyncio.CancelledError()])
async def test_pre_submission_failure_releases_lxc_reservation(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    failure: BaseException,
) -> None:
    """Exception or cancellation before POST cannot strand the reservation."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC)
    manager = mock_config_entry.runtime_data.package_manager
    with (
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_validate_snapshot",
            AsyncMock(side_effect=failure),
        ),
        patch(
            "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
            AsyncMock(),
        ) as rollback,
    ):
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: RESTORE_LXC},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert not manager.restore_reserved(*KEY)
    rollback.assert_not_awaited()


async def test_restore_reservation_blocks_buttons_during_fresh_snapshot_get(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Keep real package controls excluded throughout awaited fresh validation."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    manager._records[KEY] = PackageScanRecord(  # noqa: SLF001
        status=PackageScanStatus.SUCCESS,
        result=RESULT,
        token="token",
        reviewed=True,
    )
    manager._cleanup_evidence[KEY] = CleanupEvidence(  # noqa: SLF001
        (RemovablePackage("unused", "amd64", "1"),),
        NOW,
    )
    manager._on_state_change()  # noqa: SLF001
    await _select(hass, SELECT_LXC)

    entered = asyncio.Event()
    release = ThreadEvent()
    get = _set_snapshot_rows(
        mock_proxmox_client,
        "lxc",
        200,
        [{"name": "wanted", "snaptime": 1}],
    )

    def blocked_get() -> list[dict[str, int | str]]:
        hass.loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("test did not release fresh snapshot GET")
        return [{"name": "wanted", "snaptime": 1}]

    get.side_effect = blocked_get

    try:
        with (
            patch(
                "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
                AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS)),
            ),
            patch.object(manager, "async_start_scan", wraps=manager.async_start_scan) as scan,
            patch.object(
                manager, "async_start_update", wraps=manager.async_start_update
            ) as update,
            patch.object(
                manager,
                "async_start_autoremove",
                wraps=manager.async_start_autoremove,
            ) as autoremove,
        ):
            await hass.services.async_call(
                "button",
                SERVICE_PRESS,
                {ATTR_ENTITY_ID: RESTORE_LXC},
                blocking=True,
            )
            await asyncio.wait_for(entered.wait(), 1)

            assert manager.restore_reserved(*KEY)
            for entity_id in (SCAN_LXC, UPDATE_LXC, AUTOREMOVE_LXC):
                assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
                await hass.services.async_call(
                    "button",
                    SERVICE_PRESS,
                    {ATTR_ENTITY_ID: entity_id},
                    blocking=True,
                )
            scan.assert_not_called()
            update.assert_not_called()
            autoremove.assert_not_called()

            release.set()
            await hass.async_block_till_done()
    finally:
        release.set()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (RestoreOutcome.SUCCESS, "start was requested"),
        (RestoreOutcome.FAILED, "guest may already have changed"),
        (RestoreOutcome.UNCERTAIN, "could not be established"),
    ],
)
async def test_restore_notification_wording_and_lxc_guidance(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    outcome: RestoreOutcome,
    expected: str,
) -> None:
    """Result UX is truthful and gives fail-closed LXC recovery guidance."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC)
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        AsyncMock(return_value=RestoreResult(outcome, "UPID:test", "bounded")),
    ):
        await _press(hass, RESTORE_LXC)
    notification_id = snapshot_restore_notification_id(
        mock_config_entry.entry_id, SnapshotKind.LXC, *KEY
    )
    message = pn._async_get_or_create_notifications(hass)[notification_id][  # noqa: SLF001
        "message"
    ]
    assert expected in message
    assert "manual Scan" in message
    if outcome is RestoreOutcome.SUCCESS:
        assert "guest is running" not in message
    if outcome is RestoreOutcome.UNCERTAIN:
        assert "remain fail-closed" in message
        assert "Reload the integration only after" in message


def test_notification_ids_do_not_collide_across_entries() -> None:
    """The same cluster target has distinct result IDs per config entry."""
    first = snapshot_restore_notification_id(
        "entry-one", SnapshotKind.LXC, "pve1", 200
    )
    second = snapshot_restore_notification_id(
        "entry-two", SnapshotKind.LXC, "pve1", 200
    )
    assert first != second
    assert "entry-one" in first
    assert "entry-two" in second


@pytest.mark.parametrize(
    "permission", ["VM.Audit", "VM.Snapshot", "VM.PowerMgmt"]
)
async def test_restore_ui_requires_complete_native_permissions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    permission: str,
) -> None:
    """Both selector and start=1 Restore require Audit, Snapshot, and Power."""
    _prepare_snapshot_rows(mock_proxmox_client)
    permissions = {
        path: dict(values)
        for path, values in mock_proxmox_client.access.permissions.get.return_value.items()
    }
    permissions[f"/vms/{100}"] = {permission: 0}
    mock_proxmox_client.access.permissions.get.return_value = permissions
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get(SELECT_VM) is None
    assert hass.states.get(RESTORE_VM) is None


async def test_restore_result_notification_survives_entry_unload(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Reload guidance is not erased by the reload it instructs."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await _select(hass, SELECT_LXC)
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.UNCERTAIN, "UPID:test")),
    ):
        await _press(hass, RESTORE_LXC)
    notification_id = snapshot_restore_notification_id(
        mock_config_entry.entry_id, SnapshotKind.LXC, *KEY
    )
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert notification_id in pn._async_get_or_create_notifications(hass)  # noqa: SLF001


async def test_polish_restore_notification_path_is_localized(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The shipped Polish result path resolves without English fallback."""
    _prepare_snapshot_rows(mock_proxmox_client)
    await setup_integration(hass, mock_config_entry)
    await async_get_translations(hass, "pl", "exceptions", [DOMAIN])
    hass.config.language = "pl"
    await _select(hass, SELECT_LXC)
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_rollback_snapshot",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS)),
    ):
        await _press(hass, RESTORE_LXC)
    notification_id = snapshot_restore_notification_id(
        mock_config_entry.entry_id, SnapshotKind.LXC, *KEY
    )
    message = pn._async_get_or_create_notifications(hass)[notification_id][  # noqa: SLF001
        "message"
    ]
    assert "wycofanie do snapshota" in message
    assert "ręcznego uruchomienia Skanuj" in message
