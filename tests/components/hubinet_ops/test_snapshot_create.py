"""Tests for native snapshot Create task observation and presentation."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry, async_capture_events  # noqa: TID251

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
from homeassistant.components.button import DATA_COMPONENT, SERVICE_PRESS
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    EntityStateAttribute,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
            mock_config_entry.entry_id, SnapshotKind.QEMU, "pve1", 100, UPID
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
            mock_config_entry.entry_id, SnapshotKind.QEMU, "pve1", 100, UPID
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
        assert hass.states.get("button.vm_web_create_snapshot").attributes[
            "snapshot_create_running"
        ] is True
        task = next(
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "snapshot Create observation pve1/100"
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await hass.async_block_till_done()

    assert task.cancelled()
    assert hass.states.get("button.vm_web_create_snapshot").attributes[
        "snapshot_create_running"
    ] is False
    assert [snapshot_get.call_count for snapshot_get in snapshot_gets] == calls
    coordinator.async_request_refresh.assert_not_awaited()
    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_create_notification_id(
            mock_config_entry.entry_id, SnapshotKind.QEMU, "pve1", 100, UPID
        )
    ]
    assert "could not be confirmed" in notification["message"]
    assert "observation was interrupted" in notification["message"]


def test_create_notification_ids_are_scoped_to_entry_kind_and_guest() -> None:
    """Create result notifications cannot collide across accepted identities."""
    identities = {
        snapshot_create_notification_id(entry, kind, "pve1", vmid, UPID)
        for entry, kind, vmid in (
            ("entry-a", SnapshotKind.QEMU, 100),
            ("entry-b", SnapshotKind.QEMU, 100),
            ("entry-a", SnapshotKind.LXC, 100),
            ("entry-a", SnapshotKind.QEMU, 101),
        )
    }
    assert len(identities) == 4


def test_create_notification_ids_are_scoped_to_task_upid() -> None:
    """Concurrent Create tasks for the same guest get distinct notifications."""
    other_upid = "UPID:pve1:00000004:00000005:00000006:qmsnapshot:100:user@pam:"
    same_guest = snapshot_create_notification_id(
        "entry-a", SnapshotKind.QEMU, "pve1", 100, UPID
    )
    different_task = snapshot_create_notification_id(
        "entry-a", SnapshotKind.QEMU, "pve1", 100, other_upid
    )
    no_upid = snapshot_create_notification_id(
        "entry-a", SnapshotKind.QEMU, "pve1", 100
    )
    assert len({same_guest, different_task, no_upid}) == 3


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
            mock_config_entry.entry_id, SnapshotKind.QEMU, "pve1", 100, UPID
        )
    ]
    assert "zakończyło się pomyślnie" in notification["message"]


@pytest.mark.parametrize(
    ("outcome", "expected_text"),
    [
        (RestoreOutcome.SUCCESS, "completed successfully"),
        (RestoreOutcome.FAILED, "PVE reported an error"),
        (RestoreOutcome.UNCERTAIN, "could not be confirmed"),
        (None, "could not be confirmed"),
    ],
)
@pytest.mark.parametrize(
    ("family", "vmid", "button"),
    [
        ("qemu", 100, "button.vm_web_create_snapshot"),
        ("lxc", 200, "button.ct_nginx_create_snapshot"),
    ],
)
async def test_create_running_owns_exact_task_until_terminal_result(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    outcome: RestoreOutcome | None,
    expected_text: str,
    family: str,
    vmid: int,
    button: str,
) -> None:
    """The real button handle owns running through every terminal observation."""
    for resource, guest_id in (("qemu", 100), ("lxc", 200), ("lxc", 201)):
        _snapshot_get(mock_proxmox_client, resource, guest_id).return_value = []
    await setup_integration(hass, mock_config_entry)
    post = getattr(mock_proxmox_client._node_mock, family)(vmid).snapshot.post  # noqa: SLF001
    post.return_value = UPID
    entered = asyncio.Event()
    release = asyncio.Event()
    snapshot_get = _snapshot_get(mock_proxmox_client, family, vmid)
    initial_gets = snapshot_get.call_count

    async def blocked_observation(*_args, **_kwargs) -> RestoreResult:
        entered.set()
        await release.wait()
        if outcome is None:
            raise RuntimeError("unexpected observer error")
        return RestoreResult(outcome, UPID, "terminal evidence")

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        side_effect=blocked_observation,
    ) as observer:
        await hass.services.async_call(
            "button", SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
        )
        await asyncio.wait_for(entered.wait(), 1)
        entity = hass.data[DATA_COMPONENT].get_entity(button)
        task = entity._snapshot_create_task  # noqa: SLF001
        assert task is next(
            task
            for task in asyncio.all_tasks()
            if task.get_name() == f"snapshot Create observation pve1/{vmid}"
        )
        assert entity.snapshot_create_running is True
        assert hass.states.get(button).attributes["snapshot_create_running"] is True
        # Coordinator failure cannot hide the transient task capability.
        mock_config_entry.runtime_data.last_update_success = False
        entity.async_write_ha_state()
        assert hass.states.get(button).state == "unavailable"
        assert hass.states.get(button).attributes["snapshot_create_running"] is True
        mock_config_entry.runtime_data.last_update_success = True
        release.set()
        await task
        await hass.async_block_till_done()
        observer.assert_awaited_once()

    assert task.done()
    assert entity.snapshot_create_running is False
    assert hass.states.get(button).attributes["snapshot_create_running"] is False
    assert snapshot_get.call_count == initial_gets + (outcome is RestoreOutcome.SUCCESS)
    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_create_notification_id(
            mock_config_entry.entry_id,
            SnapshotKind.QEMU if family == "qemu" else SnapshotKind.LXC,
            "pve1",
            vmid,
            UPID,
        )
    ]
    assert expected_text in notification["message"]
    if outcome is not RestoreOutcome.SUCCESS:
        assert "completed successfully" not in notification["message"]


@pytest.mark.parametrize(
    ("family", "vmid", "button"),
    [
        ("qemu", 100, "button.vm_web_create_snapshot"),
        ("lxc", 200, "button.ct_nginx_create_snapshot"),
    ],
)
async def test_create_other_guest_observation_is_independent(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    family: str,
    vmid: int,
    button: str,
) -> None:
    """Each existing Create button retains only its own observation task."""
    await setup_integration(hass, mock_config_entry)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def observation(_proxmox, _node, upid, **_kwargs) -> RestoreResult:
        if upid == UPID:
            entered.set()
            await release.wait()
        return RestoreResult(RestoreOutcome.SUCCESS, upid)

    getattr(mock_proxmox_client._node_mock, family)(  # noqa: SLF001
        vmid
    ).snapshot.post.return_value = UPID
    other_post = mock_proxmox_client._lxc_mocks[201].snapshot.post  # noqa: SLF001
    other_post.return_value = "UPID:other"
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        side_effect=observation,
    ) as observer:
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: button},
            blocking=True,
        )
        await asyncio.wait_for(entered.wait(), 1)
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ct_backup_create_snapshot"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert hass.states.get(button).attributes["snapshot_create_running"] is True
        assert (
            hass.states.get("button.ct_backup_create_snapshot").attributes[
                "snapshot_create_running"
            ]
            is False
        )
        other_post.assert_called_once()
        assert observer.await_count == 2
        release.set()
        await hass.async_block_till_done()


async def test_eagerly_finished_create_has_no_stuck_running(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A task already done when the launcher returns publishes false immediately."""
    await setup_integration(hass, mock_config_entry)
    mock_proxmox_client._qemu_mocks[100].snapshot.post.return_value = UPID  # noqa: SLF001
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.UNCERTAIN, UPID)),
    ):
        await hass.services.async_call(
            "button",
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.vm_web_create_snapshot"},
            blocking=True,
        )
        entity = hass.data[DATA_COMPONENT].get_entity("button.vm_web_create_snapshot")
        assert entity._snapshot_create_task.done()  # noqa: SLF001
        assert entity.snapshot_create_running is False
        assert (
            hass.states.get(entity.entity_id).attributes["snapshot_create_running"]
            is False
        )
        await hass.async_block_till_done()
        assert (
            hass.states.get(entity.entity_id).attributes["snapshot_create_running"]
            is False
        )


async def _start_blocked_create(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    family: str,
    vmid: int,
    button: str,
) -> asyncio.Task[None]:
    """Press Create and return its task, held open by the caller's patch."""
    getattr(mock_proxmox_client._node_mock, family)(  # noqa: SLF001
        vmid
    ).snapshot.post.return_value = UPID
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
    )
    entity = hass.data[DATA_COMPONENT].get_entity(button)
    assert entity.snapshot_create_running is True
    return entity._snapshot_create_task  # noqa: SLF001


def _no_warnings(caplog: pytest.LogCaptureFixture) -> bool:
    return not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize(
    ("family", "vmid", "button"),
    [
        ("qemu", 100, "button.vm_web_create_snapshot"),
        ("lxc", 200, "button.ct_nginx_create_snapshot"),
    ],
)
async def test_unload_during_create_leaves_no_stale_running_fact(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    family: str,
    vmid: int,
    button: str,
) -> None:
    """A removed entity cannot publish completion, so removal clears the fact."""
    for resource, guest_id in (("qemu", 100), ("lxc", 200), ("lxc", 201)):
        _snapshot_get(mock_proxmox_client, resource, guest_id).return_value = []
    await setup_integration(hass, mock_config_entry)
    kind = SnapshotKind.QEMU if family == "qemu" else SnapshotKind.LXC

    async def blocked_observation(*_args, **_kwargs) -> RestoreResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        side_effect=blocked_observation,
    ):
        task = await _start_blocked_create(
            hass, mock_proxmox_client, family, vmid, button
        )
        old_entity = hass.data[DATA_COMPONENT].get_entity(button)
        assert hass.states.get(button).attributes["snapshot_create_running"] is True
        assert (
            entity_registry.async_get(button).capabilities["snapshot_create_running"]
            is True
        )
        caplog.clear()
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    # The restored placeholder state and the stored capability are not running.
    restored = hass.states.get(button)
    assert restored.state == STATE_UNAVAILABLE
    assert restored.attributes[EntityStateAttribute.RESTORED] is True
    assert restored.attributes["snapshot_create_running"] is False
    assert (
        entity_registry.async_get(button).capabilities["snapshot_create_running"]
        is False
    )
    # The observation still ends through its unchanged cancellation flow.
    assert task.cancelled()
    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        snapshot_create_notification_id(
            mock_config_entry.entry_id, kind, "pve1", vmid, UPID
        )
    ]
    assert "observation was interrupted" in notification["message"]
    assert _no_warnings(caplog), caplog.text

    # A normal setup creates a new entity that starts with the fact false.
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    fresh = hass.data[DATA_COMPONENT].get_entity(button)
    assert fresh is not old_entity
    assert fresh._snapshot_create_task is None  # noqa: SLF001
    assert fresh.snapshot_create_running is False
    assert hass.states.get(button).attributes["snapshot_create_running"] is False
    assert EntityStateAttribute.RESTORED not in hass.states.get(button).attributes
    assert (
        entity_registry.async_get(button).capabilities["snapshot_create_running"]
        is False
    )
    assert _no_warnings(caplog), caplog.text


async def test_reload_during_create_never_publishes_stale_running(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Across a reload every published state of the button is not running."""
    button = "button.ct_nginx_create_snapshot"
    for resource, guest_id in (("qemu", 100), ("lxc", 200), ("lxc", 201)):
        _snapshot_get(mock_proxmox_client, resource, guest_id).return_value = []
    await setup_integration(hass, mock_config_entry)

    async def blocked_observation(*_args, **_kwargs) -> RestoreResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        side_effect=blocked_observation,
    ):
        task = await _start_blocked_create(
            hass, mock_proxmox_client, "lxc", 200, button
        )
        caplog.clear()
        events = async_capture_events(hass, EVENT_STATE_CHANGED)
        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    published = [
        event.data["new_state"]
        for event in events
        if event.data["entity_id"] == button and event.data["new_state"] is not None
    ]
    assert [state.state for state in published][0] == STATE_UNAVAILABLE
    assert published[0].attributes[EntityStateAttribute.RESTORED] is True
    assert [state.attributes["snapshot_create_running"] for state in published] == [
        False
    ] * len(published)
    assert len(published) >= 2
    assert task.cancelled()
    fresh = hass.data[DATA_COMPONENT].get_entity(button)
    assert fresh.snapshot_create_running is False
    assert hass.states.get(button).attributes["snapshot_create_running"] is False
    assert (
        entity_registry.async_get(button).capabilities["snapshot_create_running"]
        is False
    )
    assert _no_warnings(caplog), caplog.text
    # The new entity accepts a new Create.
    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS, UPID)),
    ):
        await hass.services.async_call(
            "button", SERVICE_PRESS, {ATTR_ENTITY_ID: button}, blocking=True
        )
        await hass.async_block_till_done()
    assert mock_proxmox_client._lxc_mocks[200].snapshot.post.call_count == 2  # noqa: SLF001
    assert hass.states.get(button).attributes["snapshot_create_running"] is False
