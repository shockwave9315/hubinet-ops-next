"""One transient Proxmox read failure must not take the whole host offline."""

# ruff: noqa: SLF001 -- drive the shared Proxmox client mock per endpoint

from datetime import timedelta
from types import MappingProxyType
from unittest.mock import MagicMock, call, patch

from freezegun.api import FrozenDateTimeFactory
from proxmoxer.core import ResourceException
import pytest
import requests
from tests.common import MockConfigEntry, async_fire_time_changed  # noqa: TID251

from custom_components.hubinet_ops.const import CONF_VM_GUEST_MEMORY, DOMAIN
from custom_components.hubinet_ops.coordinator import DEFAULT_UPDATE_INTERVAL
from homeassistant.const import STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import setup_integration

CT_STATUS = "binary_sensor.ct_nginx_status"


@pytest.fixture(autouse=True)
def no_retry_delay():
    """Keep the real retry path without waiting in tests."""
    with patch("custom_components.hubinet_ops.coordinator.READ_RETRY_DELAY_SECONDS", 0):
        yield


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Set up, then let HA's delayed reload for re-enabled entities finish."""
    await setup_integration(hass, entry)
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _refresh(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(DEFAULT_UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _fail_then(original, *errors):
    """Raise the given errors in order, then return the original result."""
    calls = iter(errors)

    def side_effect(*args, **kwargs):
        error = next(calls, None)
        if error is not None:
            raise error
        return original

    return side_effect


@pytest.mark.parametrize(
    "error",
    [
        ResourceException(500, "Internal Server Error", "got timeout"),
        requests.exceptions.ReadTimeout("read timed out"),
    ],
)
async def test_transient_guest_list_failure_is_retried_once(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    error: Exception,
) -> None:
    """A 5xx or read timeout during Restore is absorbed by one retry."""
    await _setup(hass, mock_config_entry, freezer)
    lxc = mock_proxmox_client._node_mock.lxc.get
    lxc.side_effect = _fail_then(lxc.return_value, error)
    calls = lxc.call_count
    await _refresh(hass, freezer)
    assert lxc.call_count == calls + 2
    assert mock_config_entry.runtime_data.last_update_success is True
    assert hass.states.get(CT_STATUS).state != STATE_UNAVAILABLE


async def test_persistent_guest_list_failure_fails_with_real_error(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Old guest lists are never presented as current; the error is named."""
    await _setup(hass, mock_config_entry, freezer)
    error = ResourceException(500, "Internal Server Error", "got timeout")
    mock_proxmox_client._node_mock.lxc.get.side_effect = error
    await _refresh(hass, freezer)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.last_update_success is False
    assert coordinator.last_exception.translation_key == "api_read_failed"
    assert "got timeout" in coordinator.last_exception.translation_placeholders["error"]
    assert "nodes/pve1/lxc failed after one retry" in caplog.text
    assert hass.states.get(CT_STATUS).state == STATE_UNAVAILABLE


async def test_client_errors_are_not_retried(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 4xx is a real answer, not a transient failure."""
    await _setup(hass, mock_config_entry, freezer)
    lxc = mock_proxmox_client._node_mock.lxc.get
    lxc.side_effect = ResourceException(403, "Forbidden", "")
    calls = lxc.call_count
    await _refresh(hass, freezer)
    assert lxc.call_count == calls + 1
    assert hass.states.get(CT_STATUS).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("endpoint", ["storage", "tasks"])
async def test_informational_read_failure_keeps_the_host_online(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    endpoint: str,
) -> None:
    """Storage or backup failure keeps previous values and every guest current."""
    await _setup(hass, mock_config_entry, freezer)
    coordinator = mock_config_entry.runtime_data
    before = coordinator.data["pve1"]
    getattr(
        mock_proxmox_client._node_mock, endpoint
    ).get.side_effect = ResourceException(500, "Internal Server Error", "busy")
    await _refresh(hass, freezer)
    after = coordinator.data["pve1"]
    assert coordinator.last_update_success is True
    assert hass.states.get(CT_STATUS).state != STATE_UNAVAILABLE
    assert after.storages == before.storages
    assert after.backups == before.backups
    storage_device = dr.async_get_device_id_by_identifier(
        hass,
        (DOMAIN, f"{mock_config_entry.entry_id}_storage_local"),
        config_entry_id=mock_config_entry.entry_id,
    )
    assert storage_device is not None


# --- Checkpoint D: opt-in full QEMU listing, still one listing per node -----

FULL = call(full=1)
LIGHT = call()


def _enable_guest_memory(entry: MockConfigEntry) -> None:
    object.__setattr__(entry, "options", MappingProxyType({CONF_VM_GUEST_MEMORY: True}))


def _vms(count: int, first: int = 100) -> list[dict]:
    """Return `count` running VMs with distinct IDs."""
    return [
        {
            "vmid": first + index,
            "name": f"vm-{first + index}",
            "status": "running",
            "maxmem": 2147483648,
            "cpus": 2,
            "mem": 1073741824,
            "cpu": 0.1,
            "maxdisk": 34359738368,
            "disk": 1234567890,
            "uptime": 60,
            "netin": 1,
            "netout": 1,
        }
        for index in range(count)
    ]


def _three_nodes(client: MagicMock, vms_per_node: int) -> dict[str, MagicMock]:
    """Serve pve1 and pve2 (online) and pve3 (offline), each with its own API."""
    apis: dict[str, MagicMock] = {}
    for index, name in enumerate(("pve1", "pve2", "pve3")):
        api = MagicMock()
        api.qemu.get.return_value = _vms(vms_per_node, first=100 + 1000 * index)
        api.lxc.get.return_value = []
        api.storage.get.return_value = []
        api.tasks.get.return_value = []
        apis[name] = api
    client.nodes.get.return_value = client._all_nodes
    client.nodes.side_effect = lambda name: apis[name]
    return apis


async def _sensors_only(
    hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """Set up only telemetry, so no per-guest snapshot selector is involved."""
    with patch("custom_components.hubinet_ops.PLATFORMS", [Platform.SENSOR]):
        await _setup(hass, entry, freezer)


@pytest.mark.parametrize(("enabled", "expected"), [(False, LIGHT), (True, FULL)])
async def test_qemu_listing_parameter_follows_the_option(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    enabled: bool,
    expected: object,
) -> None:
    """Off sends no `full` parameter at all; on sends exactly full=1."""
    if enabled:
        _enable_guest_memory(mock_config_entry)
    await _sensors_only(hass, mock_config_entry, freezer)
    listing = mock_proxmox_client._node_mock.qemu.get
    assert mock_config_entry.runtime_data.vm_guest_memory is enabled
    assert listing.call_args_list
    assert all(used == expected for used in listing.call_args_list)
    before = listing.call_count
    await _refresh(hass, freezer)
    assert listing.call_args_list[before:] == [expected]
    # The LXC listing is not affected by the option.
    lxc = mock_proxmox_client._node_mock.lxc.get
    assert all(used == LIGHT for used in lxc.call_args_list)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("vms_per_node", [1, 30])
async def test_one_qemu_listing_per_online_node_and_no_per_vm_request(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    enabled: bool,
    vms_per_node: int,
) -> None:
    """Request count depends on online nodes only, never on the VM count."""
    if enabled:
        _enable_guest_memory(mock_config_entry)
    apis = _three_nodes(mock_proxmox_client, vms_per_node)
    await _sensors_only(hass, mock_config_entry, freezer)
    coordinator = mock_config_entry.runtime_data
    assert {name: len(node.vms) for name, node in coordinator.data.items()} == {
        "pve1": vms_per_node,
        "pve2": vms_per_node,
        "pve3": 0,
    }

    before = {name: api.qemu.get.call_count for name, api in apis.items()}
    await _refresh(hass, freezer)
    assert coordinator.last_update_success is True
    expected = FULL if enabled else LIGHT
    for name in ("pve1", "pve2"):
        listing = apis[name].qemu.get
        assert listing.call_args_list[before[name] :] == [expected]
    for name, api in apis.items():
        # `api.qemu(vmid)` would be a per-VM resource such as /status/current.
        api.qemu.assert_not_called()
        api.lxc.assert_not_called()
        assert name != "pve3" or api.mock_calls == []
    # An offline node is never asked for its guests.
    assert apis["pve3"].qemu.get.call_count == 0


async def test_full_listing_keeps_the_single_retry(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A transient failure repeats the same full listing once, nothing more."""
    _enable_guest_memory(mock_config_entry)
    await _sensors_only(hass, mock_config_entry, freezer)
    listing = mock_proxmox_client._node_mock.qemu.get
    error = ResourceException(500, "Internal Server Error", "got timeout")
    listing.side_effect = _fail_then(listing.return_value, error)
    before = listing.call_count
    await _refresh(hass, freezer)
    assert listing.call_args_list[before:] == [FULL, FULL]
    assert mock_config_entry.runtime_data.last_update_success is True

    listing.side_effect = error
    before = listing.call_count
    await _refresh(hass, freezer)
    assert listing.call_args_list[before:] == [FULL, FULL]
    coordinator = mock_config_entry.runtime_data
    assert coordinator.last_update_success is False
    assert coordinator.last_exception.translation_key == "api_read_failed"

    listing.side_effect = ResourceException(403, "Forbidden", "")
    before = listing.call_count
    await _refresh(hass, freezer)
    assert listing.call_args_list[before:] == [FULL]
    mock_proxmox_client._node_mock.qemu.assert_not_called()
