"""Tests for presentation-only native snapshot selection."""

import asyncio
from copy import deepcopy
from threading import Event as ThreadEvent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from syrupy.assertion import SnapshotAssertion
from tests.common import (  # noqa: TID251
    MockConfigEntry,
    async_fire_time_changed,
    snapshot_platform,
)

from custom_components.hubinet_ops.select import (
    ATTR_SELECTED_SNAPSHOT,
    PARALLEL_UPDATES,
    SCAN_INTERVAL,
    snapshot_select_unique_id,
)
from custom_components.hubinet_ops.snapshots import (
    NativeSnapshot,
    RestoreOutcome,
    RestoreResult,
)
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.homeassistant import (
    DOMAIN as HA_DOMAIN,
    SERVICE_UPDATE_ENTITY,
)
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform as ep, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from . import setup_integration

SELECT_VM = "select.vm_web_snapshot_to_restore"


def _set_snapshot_rows(
    mock_proxmox_client: MagicMock, family: str, vmid: int, rows: object
) -> MagicMock:
    """Set the cached proxmoxer snapshot listing used by one guest."""
    guest = getattr(mock_proxmox_client._node_mock, family)(vmid)  # noqa: SLF001
    guest.snapshot.get.return_value = rows
    return guest.snapshot.get


async def _update_entity(hass: HomeAssistant, entity_id: str) -> None:
    """Call the ordinary Home Assistant manual entity update action."""
    await async_setup_component(hass, HA_DOMAIN, {})
    await hass.services.async_call(
        HA_DOMAIN,
        SERVICE_UPDATE_ENTITY,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> None:
    """Enable config-category selects in tests."""


async def test_all_select_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Snapshot the additive select platform entity registry surface."""
    for family, vmid in (("qemu", 100), ("lxc", 200), ("lxc", 201)):
        _set_snapshot_rows(mock_proxmox_client, family, vmid, [])
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "custom_components.hubinet_ops.PLATFORMS", [Platform.SELECT]
        )
        await setup_integration(hass, mock_config_entry)
        await snapshot_platform(
            hass, entity_registry, snapshot, mock_config_entry.entry_id
        )


async def test_select_owns_polling_and_has_no_default(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Poll only snapshots and keep operator choice empty by default."""
    get = _set_snapshot_rows(
        mock_proxmox_client,
        "qemu",
        100,
        [
            {"name": "old", "snaptime": 1},
            {"name": "new", "snaptime": 2},
        ],
    )
    _set_snapshot_rows(mock_proxmox_client, "lxc", 200, [])
    _set_snapshot_rows(mock_proxmox_client, "lxc", 201, [])
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(SELECT_VM)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes["options"] == ["new", "old"]
    assert state.attributes[ATTR_SELECTED_SNAPSHOT] is None

    coordinator = mock_config_entry.runtime_data
    coordinator.async_request_refresh = AsyncMock()
    calls = get.call_count
    await _update_entity(hass, SELECT_VM)
    assert get.call_count == calls + 1
    coordinator.async_request_refresh.assert_not_awaited()
    select_platform = next(
        platform
        for platform in ep.async_get_platforms(hass, "hubinet_ops")
        if platform.domain == "select"
    )
    assert select_platform.entities[SELECT_VM].should_poll is True


def test_select_unique_id_survives_guest_node_change() -> None:
    """Persistent selector identity follows guest VMID, not its current node."""
    ids_by_node = {
        node: snapshot_select_unique_id("entry", 100) for node in ("pve1", "pve2")
    }
    assert set(ids_by_node.values()) == {"entry_100_snapshot_to_restore"}


async def test_initial_snapshot_get_does_not_block_entry_setup(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Schedule optional initial snapshot I/O after normal entry setup."""
    entered = asyncio.Event()
    release = ThreadEvent()

    def blocked_get() -> list[dict[str, int | str]]:
        hass.loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("test did not release snapshot GET")
        return [{"name": "wanted", "snaptime": 1}]

    get = _set_snapshot_rows(mock_proxmox_client, "qemu", 100, [])
    get.side_effect = blocked_get
    _set_snapshot_rows(mock_proxmox_client, "lxc", 200, [])
    _set_snapshot_rows(mock_proxmox_client, "lxc", 201, [])
    mock_config_entry.add_to_hass(hass)
    try:
        assert await asyncio.wait_for(
            hass.config_entries.async_setup(mock_config_entry.entry_id), 1
        )
        await asyncio.wait_for(entered.wait(), 1)

        state = hass.states.get(SELECT_VM)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE
        assert state.attributes["options"] == []
        assert hass.states.get("sensor.vm_web_cpu_usage") is not None

        coordinator = mock_config_entry.runtime_data
        coordinator.async_request_refresh = AsyncMock()
        release.set()
        await hass.async_block_till_done()

        state = hass.states.get(SELECT_VM)
        assert state is not None
        assert state.state == STATE_UNKNOWN
        assert state.attributes["options"] == ["wanted"]
        coordinator.async_request_refresh.assert_not_awaited()
    finally:
        release.set()


async def test_selection_is_not_restored_after_entry_reload(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The operator choice remains ephemeral across integration reconstruction."""
    _set_snapshot_rows(
        mock_proxmox_client,
        "qemu",
        100,
        [{"name": "wanted", "snaptime": 1}],
    )
    _set_snapshot_rows(mock_proxmox_client, "lxc", 200, [])
    _set_snapshot_rows(mock_proxmox_client, "lxc", 201, [])
    await setup_integration(hass, mock_config_entry)
    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: SELECT_VM, ATTR_OPTION: "wanted"},
        blocking=True,
    )
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(SELECT_VM)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_SELECTED_SNAPSHOT] is None


@pytest.mark.parametrize("snapshot_name", ["unknown", "unavailable"])
async def test_selected_snapshot_attribute_avoids_ha_sentinel_collision(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    snapshot_name: str,
) -> None:
    """Preserve legal PVE names even when HA interprets state text specially."""
    _set_snapshot_rows(
        mock_proxmox_client,
        "qemu",
        100,
        [{"name": snapshot_name, "snaptime": 1}],
    )
    _set_snapshot_rows(mock_proxmox_client, "lxc", 200, [])
    _set_snapshot_rows(mock_proxmox_client, "lxc", 201, [])
    await setup_integration(hass, mock_config_entry)

    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: SELECT_VM, ATTR_OPTION: snapshot_name},
        blocking=True,
    )

    state = hass.states.get(SELECT_VM)
    assert state is not None
    assert state.attributes[ATTR_SELECTED_SNAPSHOT] == snapshot_name


async def test_disappearing_selection_clears_only_local_choice(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Clear a choice after a successful fresh list no longer includes it."""
    get = _set_snapshot_rows(
        mock_proxmox_client,
        "qemu",
        100,
        [{"name": "wanted", "snaptime": 1}],
    )
    _set_snapshot_rows(mock_proxmox_client, "lxc", 200, [])
    _set_snapshot_rows(mock_proxmox_client, "lxc", 201, [])
    await setup_integration(hass, mock_config_entry)
    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: SELECT_VM, ATTR_OPTION: "wanted"},
        blocking=True,
    )

    get.return_value = [{"name": "other", "snaptime": 2}]
    await _update_entity(hass, SELECT_VM)

    state = hass.states.get(SELECT_VM)
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_SELECTED_SNAPSHOT] is None
    assert state.attributes["options"] == ["other"]


async def test_snapshot_poll_failure_isolated_from_main_entities(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Optional endpoint failure does not refresh or poison native core state."""
    get = _set_snapshot_rows(
        mock_proxmox_client,
        "qemu",
        100,
        [{"name": "wanted", "snaptime": 1}],
    )
    _set_snapshot_rows(mock_proxmox_client, "lxc", 200, [])
    _set_snapshot_rows(mock_proxmox_client, "lxc", 201, [])
    await setup_integration(hass, mock_config_entry)
    coordinator = mock_config_entry.runtime_data
    coordinator.async_request_refresh = AsyncMock()
    get.side_effect = ConnectionError("snapshot endpoint failed")

    await _update_entity(hass, SELECT_VM)

    state = hass.states.get(SELECT_VM)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    assert state.attributes["options"] == ["wanted"]
    assert hass.states.get("sensor.vm_web_cpu_usage").state != STATE_UNAVAILABLE
    coordinator.async_request_refresh.assert_not_awaited()


async def test_native_platform_bounds_initial_and_periodic_snapshot_polling(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Bound selector waves to three without blocking Restore or coordinator I/O."""
    assert PARALLEL_UPDATES == 3
    node = mock_proxmox_client._node_mock  # noqa: SLF001
    template = node.qemu.get.return_value[0]
    vms = [
        {**deepcopy(template), "vmid": 300 + index, "name": f"hotfix-{index}"}
        for index in range(6)
    ]
    node.qemu.get.return_value = vms
    node.lxc.get.return_value = []
    resources: dict[int, MagicMock] = {}

    def qemu_resource(vmid: int) -> MagicMock:
        resource = resources.setdefault(vmid, MagicMock())
        resource.snapshot.get.return_value = [
            {"name": "wanted", "snaptime": 1}
        ]
        return resource

    node.qemu.side_effect = qemu_resource
    expected_updates = len(vms)

    def new_wave() -> dict[str, object]:
        return {
            "active": 0,
            "peak": 0,
            "completed": 0,
            "full": asyncio.Event(),
            "done": asyncio.Event(),
            "release": asyncio.Event(),
        }

    wave = new_wave()

    async def blocked_snapshot_list(*_args, **_kwargs) -> tuple[NativeSnapshot, ...]:
        wave["active"] += 1
        wave["peak"] = max(wave["peak"], wave["active"])
        if wave["active"] == PARALLEL_UPDATES:
            wave["full"].set()
        await wave["release"].wait()
        wave["active"] -= 1
        wave["completed"] += 1
        if wave["completed"] == expected_updates:
            wave["done"].set()
        return (NativeSnapshot("wanted", 1),)

    mock_config_entry.add_to_hass(hass)
    try:
        with patch(
            "custom_components.hubinet_ops.select.async_list_snapshots",
            side_effect=blocked_snapshot_list,
        ):
            assert await asyncio.wait_for(
                hass.config_entries.async_setup(mock_config_entry.entry_id), 1
            )
            await asyncio.wait_for(wave["full"].wait(), 1)
            assert wave["peak"] == PARALLEL_UPDATES
            wave["release"].set()
            await asyncio.wait_for(wave["done"].wait(), 1)
            await hass.async_block_till_done()

            await hass.services.async_call(
                "select",
                SERVICE_SELECT_OPTION,
                {
                    ATTR_ENTITY_ID: "select.hotfix_5_snapshot_to_restore",
                    ATTR_OPTION: "wanted",
                },
                blocking=True,
            )

            wave = new_wave()
            async_fire_time_changed(hass, dt_util.utcnow() + SCAN_INTERVAL)
            await asyncio.wait_for(wave["full"].wait(), 1)
            assert wave["peak"] == PARALLEL_UPDATES

            restore_started = asyncio.Event()

            async def restore_while_selects_wait(
                *_args, **_kwargs
            ) -> RestoreResult:
                restore_started.set()
                return RestoreResult(RestoreOutcome.SUCCESS)

            with patch(
                "custom_components.hubinet_ops.snapshot_restore."
                "async_rollback_snapshot",
                side_effect=restore_while_selects_wait,
            ):
                await hass.services.async_call(
                    "button",
                    SERVICE_PRESS,
                    {ATTR_ENTITY_ID: "button.hotfix_5_restore"},
                    blocking=True,
                )
                await asyncio.wait_for(restore_started.wait(), 1)

            coordinator = mock_config_entry.runtime_data
            await asyncio.wait_for(coordinator.async_request_refresh(), 1)
            assert wave["active"] == PARALLEL_UPDATES

            wave["release"].set()
            await asyncio.wait_for(wave["done"].wait(), 1)
            assert wave["completed"] == expected_updates
            assert wave["peak"] == PARALLEL_UPDATES
    finally:
        for event_name in ("release", "done"):
            event = wave[event_name]
            if isinstance(event, asyncio.Event):
                event.set()
