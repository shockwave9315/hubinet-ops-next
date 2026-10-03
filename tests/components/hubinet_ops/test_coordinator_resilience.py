"""One transient Proxmox read failure must not take the whole host offline."""

# ruff: noqa: SLF001 -- drive the shared Proxmox client mock per endpoint

from datetime import timedelta
from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from proxmoxer.core import ResourceException
import pytest
import requests
from tests.common import MockConfigEntry, async_fire_time_changed  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.coordinator import DEFAULT_UPDATE_INTERVAL
from homeassistant.const import STATE_UNAVAILABLE
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
