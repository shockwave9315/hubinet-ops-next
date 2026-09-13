"""Tests for the ProxmoxVE button platform."""

import asyncio
import re
from unittest.mock import ANY, MagicMock, patch

from proxmoxer import AuthenticationError
from proxmoxer.core import ResourceException
import pytest
from requests.exceptions import ConnectTimeout, SSLError
from syrupy.assertion import SnapshotAssertion
from tests.common import MockConfigEntry, snapshot_platform

from custom_components.hubinet_ops.snapshots import (
    RestoreOutcome,
    RestoreResult,
    SnapshotKind,
)
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from . import AUDIT_PERMISSIONS, setup_integration

BUTTON_DOMAIN = "button"
UPID = "UPID:pve1:00000001:00000002:00000003:qmsnapshot:100:user@pam:"
UPID = "UPID:pve1:00000001:00000002:00000003:qmsnapshot:100:user@pam:"


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> None:
    """Enable all entities for button tests."""


async def test_all_button_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Snapshot test for all ProxmoxVE button entities."""
    with patch(
        "custom_components.hubinet_ops.PLATFORMS",
        [Platform.BUTTON],
    ):
        await setup_integration(hass, mock_config_entry)
        await snapshot_platform(
            hass, entity_registry, snapshot, mock_config_entry.entry_id
        )


@pytest.mark.parametrize(
    ("entity_id", "command"),
    [
        ("button.pve1_restart", "reboot"),
        ("button.pve1_shut_down", "shutdown"),
    ],
)
async def test_node_buttons(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    command: str,
) -> None:
    """Test pressing a ProxmoxVE node action button triggers the correct API call."""
    await setup_integration(hass, mock_config_entry)

    method_mock = mock_proxmox_client._node_mock.status.post
    pre_calls = len(method_mock.mock_calls)

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    assert len(method_mock.mock_calls) == pre_calls + 1
    method_mock.assert_called_with(command=command)


@pytest.mark.parametrize(
    ("entity_id", "attr"),
    [
        ("button.pve1_start_all", "startall"),
        ("button.pve1_stop_all", "stopall"),
        ("button.pve1_suspend_all", "suspendall"),
    ],
)
async def test_node_all_actions_buttons(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    attr: str,
) -> None:
    """Test ProxmoxVE node start/stop all button API call."""
    await setup_integration(hass, mock_config_entry)

    method_mock = getattr(mock_proxmox_client._node_mock, attr).post
    pre_calls = len(method_mock.mock_calls)

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    assert len(method_mock.mock_calls) == pre_calls + 1


@pytest.mark.parametrize(
    ("entity_id", "vmid", "action"),
    [
        ("button.vm_web_start", 100, "start"),
        ("button.vm_web_stop", 100, "stop"),
        ("button.vm_web_restart", 100, "reboot"),
        ("button.vm_web_suspend", 100, "suspend"),
        ("button.vm_web_reset", 100, "reset"),
        ("button.vm_web_shut_down", 100, "shutdown"),
    ],
)
async def test_vm_buttons(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    vmid: int,
    action: str,
) -> None:
    """Test pressing a ProxmoxVE VM action button triggers the correct API call."""
    await setup_integration(hass, mock_config_entry)

    mock_proxmox_client._node_mock.qemu(vmid)
    method_mock = getattr(mock_proxmox_client._qemu_mocks[vmid].status, action).post
    pre_calls = len(method_mock.mock_calls)

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    assert len(method_mock.mock_calls) == pre_calls + 1


@pytest.mark.parametrize(
    ("entity_id", "vmid", "guest_resource", "kind"),
    [
        pytest.param(
            "button.vm_web_create_snapshot", 100, "qemu", SnapshotKind.QEMU, id="vm"
        ),
        pytest.param(
            "button.ct_nginx_create_snapshot",
            200,
            "lxc",
            SnapshotKind.LXC,
            id="container",
        ),
    ],
)
async def test_snapshot_button(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    vmid: int,
    guest_resource: str,
    kind: SnapshotKind,
) -> None:
    """Preserve the native Create POST and pass its UPID to the observer hook."""
    await setup_integration(hass, mock_config_entry)

    node = mock_proxmox_client.nodes("pve1")
    method_mock = getattr(node, guest_resource)(vmid).snapshot.post
    method_mock.return_value = UPID

    with patch(
        "custom_components.hubinet_ops.button.start_snapshot_create_observation"
    ) as start_observation:
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    method_mock.assert_called_once_with(snapname=ANY)
    start_observation.assert_called_once_with(
        hass,
        mock_config_entry.runtime_data,
        kind,
        "pve1",
        vmid,
        UPID,
    )

    # Proxmox validates the name as a `pve-configid` of at most 40 characters:
    # two or more, starting with a letter, then only [A-Za-z0-9_-]
    name = method_mock.call_args.kwargs["snapname"]
    assert len(name) <= 40
    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]+", name)


@pytest.mark.parametrize(
    ("entity_id", "vmid", "guest_resource"),
    [
        pytest.param("button.vm_web_create_snapshot", 100, "qemu", id="vm"),
        pytest.param("button.ct_nginx_create_snapshot", 200, "lxc", id="container"),
    ],
)
async def test_create_button_returns_while_observation_remains_blocked(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    vmid: int,
    guest_resource: str,
) -> None:
    """Release the button semaphore after POST, before terminal observation."""
    await setup_integration(hass, mock_config_entry)
    getattr(mock_proxmox_client._node_mock, guest_resource)(  # noqa: SLF001
        vmid
    ).snapshot.post.return_value = UPID
    observation_entered = asyncio.Event()
    observation_release = asyncio.Event()

    async def blocked_observation(*_args, **_kwargs) -> RestoreResult:
        observation_entered.set()
        await observation_release.wait()
        return RestoreResult(RestoreOutcome.SUCCESS, UPID)

    with patch(
        "custom_components.hubinet_ops.snapshot_restore.async_observe_task",
        side_effect=blocked_observation,
    ):
        await asyncio.wait_for(
            hass.services.async_call(
                BUTTON_DOMAIN,
                SERVICE_PRESS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            ),
            1,
        )
        await asyncio.wait_for(observation_entered.wait(), 1)

        start = mock_proxmox_client._qemu_mocks[100].status.start.post  # noqa: SLF001
        prior_calls = start.call_count
        await asyncio.wait_for(
            hass.services.async_call(
                BUTTON_DOMAIN,
                SERVICE_PRESS,
                {ATTR_ENTITY_ID: "button.vm_web_start"},
                blocking=True,
            ),
            1,
        )
        assert start.call_count == prior_calls + 1

        observation_release.set()
        await hass.async_block_till_done()


async def test_non_snapshot_native_buttons_never_start_create_observation(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Keep the post-result hook exclusive to native snapshot Create."""
    await setup_integration(hass, mock_config_entry)

    with patch(
        "custom_components.hubinet_ops.button.start_snapshot_create_observation"
    ) as start_observation:
        for entity_id in ("button.vm_web_start", "button.ct_nginx_start"):
            await hass.services.async_call(
                BUTTON_DOMAIN,
                SERVICE_PRESS,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )

    start_observation.assert_not_called()


@pytest.mark.parametrize(
    ("entity_id", "vmid", "guest_resource"),
    [
        pytest.param("button.vm_web_create_snapshot", 100, "qemu", id="vm"),
        pytest.param("button.ct_nginx_create_snapshot", 200, "lxc", id="container"),
    ],
)
async def test_create_post_error_keeps_upstream_mapping_and_starts_no_observer(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    vmid: int,
    guest_resource: str,
) -> None:
    """A synchronous native POST error remains a button error without observer."""
    await setup_integration(hass, mock_config_entry)
    getattr(mock_proxmox_client._node_mock, guest_resource)(  # noqa: SLF001
        vmid
    ).snapshot.post.side_effect = ResourceException(500, "error", {})

    with (
        patch(
            "custom_components.hubinet_ops.button.start_snapshot_create_observation"
        ) as start_observation,
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    start_observation.assert_not_called()


async def test_create_observer_start_failure_does_not_recast_accepted_post(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Do not report a successful native POST as failed if task tracking fails."""
    await setup_integration(hass, mock_config_entry)
    post = mock_proxmox_client._qemu_mocks[100].snapshot.post  # noqa: SLF001
    post.return_value = UPID

    with patch.object(
        mock_config_entry,
        "async_create_background_task",
        side_effect=RuntimeError("task tracking failed"),
    ):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.vm_web_create_snapshot"},
            blocking=True,
        )

    post.assert_called_once_with(snapname=ANY)


@pytest.mark.parametrize(
    ("entity_id", "vmid", "action"),
    [
        ("button.ct_nginx_start", 200, "start"),
        ("button.ct_nginx_stop", 200, "stop"),
        ("button.ct_nginx_restart", 200, "reboot"),
    ],
)
async def test_container_buttons(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    vmid: int,
    action: str,
) -> None:
    """Test ProxmoxVE container action button API call."""
    await setup_integration(hass, mock_config_entry)

    mock_proxmox_client._node_mock.lxc(vmid)
    if action == "snapshot":
        method_mock = mock_proxmox_client._lxc_mocks[vmid].snapshot.post
    else:
        method_mock = getattr(mock_proxmox_client._lxc_mocks[vmid].status, action).post
    pre_calls = len(method_mock.mock_calls)

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    assert len(method_mock.mock_calls) == pre_calls + 1


@pytest.mark.parametrize(
    ("entity_id", "exception"),
    [
        ("button.pve1_restart", AuthenticationError("auth failed")),
        ("button.pve1_restart", SSLError("ssl error")),
        ("button.pve1_restart", ConnectTimeout("timeout")),
        ("button.pve1_shut_down", ResourceException(500, "error", {})),
    ],
)
async def test_node_buttons_exceptions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    exception: Exception,
) -> None:
    """Test that ProxmoxVE node button errors are raised as HomeAssistantError."""
    await setup_integration(hass, mock_config_entry)

    mock_proxmox_client._node_mock.status.post.side_effect = exception

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )


@pytest.mark.parametrize(
    ("entity_id", "vmid", "action", "exception"),
    [
        (
            "button.vm_web_start",
            100,
            "start",
            AuthenticationError("auth failed"),
        ),
        (
            "button.vm_web_start",
            100,
            "start",
            SSLError("ssl error"),
        ),
        (
            "button.vm_web_suspend",
            100,
            "suspend",
            ConnectTimeout("timeout"),
        ),
        (
            "button.vm_web_reset",
            100,
            "reset",
            ResourceException(500, "error", {}),
        ),
    ],
)
async def test_vm_buttons_exceptions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    vmid: int,
    action: str,
    exception: Exception,
) -> None:
    """Test that ProxmoxVE VM button errors are raised as HomeAssistantError."""
    await setup_integration(hass, mock_config_entry)

    mock_proxmox_client._node_mock.qemu(vmid)
    getattr(
        mock_proxmox_client._qemu_mocks[vmid].status, action
    ).post.side_effect = exception

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )


@pytest.mark.parametrize(
    ("entity_id", "vmid", "action", "exception"),
    [
        (
            "button.ct_nginx_start",
            200,
            "start",
            AuthenticationError("auth failed"),
        ),
        (
            "button.ct_nginx_start",
            200,
            "start",
            SSLError("ssl error"),
        ),
        (
            "button.ct_nginx_restart",
            200,
            "reboot",
            ConnectTimeout("timeout"),
        ),
        (
            "button.ct_nginx_stop",
            200,
            "stop",
            ResourceException(500, "error", {}),
        ),
    ],
)
async def test_container_buttons_exceptions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_id: str,
    vmid: int,
    action: str,
    exception: Exception,
) -> None:
    """Test that ProxmoxVE container button errors are raised as HomeAssistantError."""
    await setup_integration(hass, mock_config_entry)

    mock_proxmox_client._node_mock.lxc(vmid)
    getattr(
        mock_proxmox_client._lxc_mocks[vmid].status, action
    ).post.side_effect = exception

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )


async def test_buttons_only_allowed_buttons(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test that ProxmoxVE button is not generated when not allowed."""
    mock_proxmox_client.access.permissions.get.return_value = AUDIT_PERMISSIONS

    await setup_integration(hass, mock_config_entry)

    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )

    assert all(not entry.entity_id.startswith("button.") for entry in entries)
