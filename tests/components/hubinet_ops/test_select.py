"""Tests for presentation-only native snapshot selection."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from syrupy.assertion import SnapshotAssertion
from tests.common import MockConfigEntry, snapshot_platform  # noqa: TID251

from custom_components.hubinet_ops.select import ATTR_SELECTED_SNAPSHOT
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
    get.return_value[0]["name"] = "changed-locally"
    assert mock_proxmox_client._qemu_mocks[100].snapshot.post.call_count == 0  # noqa: SLF001


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
