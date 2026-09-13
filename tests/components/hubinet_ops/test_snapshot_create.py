"""Tests for native snapshot Create task observation and presentation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.snapshot_restore import (
    snapshot_create_notification_id,
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
from homeassistant.helpers.translation import async_get_translations

from . import setup_integration

UPID = "UPID:pve1:00000001:00000002:00000003:qmsnapshot:100:user@pam:"
SELECT_VM = "select.vm_web_snapshot_to_restore"


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> None:
    """Enable config-category snapshot controls in tests."""


def _snapshot_get(
    mock_proxmox_client: MagicMock, family: str, vmid: int
) -> MagicMock:
    """Return one guest's mocked native snapshot listing call."""
    return getattr(mock_proxmox_client._node_mock, family)(  # noqa: SLF001
        vmid
    ).snapshot.get


async def test_create_success_notifies_and_refreshes_only_exact_selector(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Refresh the committed guest snapshot list while preserving its choice."""
    vm_get = _snapshot_get(mock_proxmox_client, "qemu", 100)
    vm_get.return_value = [{"name": "existing", "snaptime": 1}]
    lxc_gets = [
        _snapshot_get(mock_proxmox_client, "lxc", vmid) for vmid in (200, 201)
    ]
    for snapshot_get in lxc_gets:
        snapshot_get.return_value = []
    await setup_integration(hass, mock_config_entry)
    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: SELECT_VM, ATTR_OPTION: "existing"},
        blocking=True,
    )

    vm_calls = vm_get.call_count
    other_calls = [snapshot_get.call_count for snapshot_get in lxc_gets]
    vm_get.return_value = [
        {"name": "created", "snaptime": 2},
        {"name": "existing", "snaptime": 1},
    ]
    mock_proxmox_client._qemu_mocks[100].snapshot.post.return_value = UPID  # noqa: SLF001
    coordinator = mock_config_entry.runtime_data
    coordinator.async_request_refresh = AsyncMock()

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS, UPID)),
    ):
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.vm_web_create_snapshot"},
            blocking=True,
        )
        await hass.async_block_till_done()

    state = hass.states.get(SELECT_VM)
    assert state is not None
    assert state.attributes["options"] == ["created", "existing"]
    assert state.attributes["selected_snapshot"] == "existing"
    assert vm_get.call_count == vm_calls + 1
    assert [snapshot_get.call_count for snapshot_get in lxc_gets] == other_calls
    coordinator.async_request_refresh.assert_not_awaited()

    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_create_notification_id(
            mock_config_entry.entry_id, SnapshotKind.QEMU, "pve1", 100
        )
    ]
    assert "completed successfully" in notification["message"]
    assert UPID in notification["message"]


@pytest.mark.parametrize(
    ("result", "expected_text"),
    [
        (
            RestoreResult(RestoreOutcome.FAILED, UPID, "snapshot failed"),
            "PVE reported an error",
        ),
        (
            RestoreResult(RestoreOutcome.UNCERTAIN, UPID, "status unavailable"),
            "could not be confirmed",
        ),
    ],
)
async def test_create_non_success_notifies_without_any_refresh(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    result: RestoreResult,
    expected_text: str,
) -> None:
    """FAILED and UNCERTAIN publish truth without forcing selector/coordinator I/O."""
    snapshot_gets = [
        _snapshot_get(mock_proxmox_client, family, vmid)
        for family, vmid in (("qemu", 100), ("lxc", 200), ("lxc", 201))
    ]
    for snapshot_get in snapshot_gets:
        snapshot_get.return_value = []
    await setup_integration(hass, mock_config_entry)
    calls = [snapshot_get.call_count for snapshot_get in snapshot_gets]
    mock_proxmox_client._qemu_mocks[100].snapshot.post.return_value = UPID  # noqa: SLF001
    coordinator = mock_config_entry.runtime_data
    coordinator.async_request_refresh = AsyncMock()

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        AsyncMock(return_value=result),
    ):
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.vm_web_create_snapshot"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert [snapshot_get.call_count for snapshot_get in snapshot_gets] == calls
    coordinator.async_request_refresh.assert_not_awaited()
    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_create_notification_id(
            mock_config_entry.entry_id, SnapshotKind.QEMU, "pve1", 100
        )
    ]
    assert expected_text in notification["message"]
    assert result.reason in notification["message"]


async def test_create_observation_cancellation_notifies_uncertain_and_propagates(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Cancellation after POST remains visible and cancels the tracked task."""
    snapshot_gets = [
        _snapshot_get(mock_proxmox_client, family, vmid)
        for family, vmid in (("qemu", 100), ("lxc", 200), ("lxc", 201))
    ]
    for snapshot_get in snapshot_gets:
        snapshot_get.return_value = []
    await setup_integration(hass, mock_config_entry)
    calls = [snapshot_get.call_count for snapshot_get in snapshot_gets]
    mock_proxmox_client._qemu_mocks[100].snapshot.post.return_value = UPID  # noqa: SLF001
    coordinator = mock_config_entry.runtime_data
    coordinator.async_request_refresh = AsyncMock()
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked_observation(*_args, **_kwargs) -> RestoreResult:
        entered.set()
        await never.wait()
        raise AssertionError("unreachable")

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        side_effect=blocked_observation,
    ):
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.vm_web_create_snapshot"},
            blocking=True,
        )
        await asyncio.wait_for(entered.wait(), 1)
        task = next(
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "snapshot Create observation pve1/100"
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelled()
    assert [snapshot_get.call_count for snapshot_get in snapshot_gets] == calls
    coordinator.async_request_refresh.assert_not_awaited()
    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_create_notification_id(
            mock_config_entry.entry_id, SnapshotKind.QEMU, "pve1", 100
        )
    ]
    assert "could not be confirmed" in notification["message"]
    assert "observation was interrupted" in notification["message"]


def test_create_notification_ids_are_scoped_to_entry_kind_and_guest() -> None:
    """Create result notifications cannot collide across accepted identities."""
    identities = {
        snapshot_create_notification_id(entry, kind, "pve1", vmid)
        for entry, kind, vmid in (
            ("entry-a", SnapshotKind.QEMU, 100),
            ("entry-b", SnapshotKind.QEMU, 100),
            ("entry-a", SnapshotKind.LXC, 100),
            ("entry-a", SnapshotKind.QEMU, 101),
        )
    }
    assert len(identities) == 4


async def test_polish_create_notification_path_is_localized(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Resolve the shipped Polish terminal Create notification path."""
    for family, vmid in (("qemu", 100), ("lxc", 200), ("lxc", 201)):
        _snapshot_get(mock_proxmox_client, family, vmid).return_value = []
    await setup_integration(hass, mock_config_entry)
    await async_get_translations(hass, "pl", "exceptions", [DOMAIN])
    hass.config.language = "pl"
    mock_proxmox_client._qemu_mocks[100].snapshot.post.return_value = UPID  # noqa: SLF001

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS, UPID)),
    ):
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.vm_web_create_snapshot"},
            blocking=True,
        )
        await hass.async_block_till_done()

    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_create_notification_id(
            mock_config_entry.entry_id, SnapshotKind.QEMU, "pve1", 100
        )
    ]
    assert "zakończyło się pomyślnie" in notification["message"]
