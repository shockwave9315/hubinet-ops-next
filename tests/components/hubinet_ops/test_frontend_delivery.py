"""The dashboard card ships with the integration without a manual resource."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import INTEGRATION_VERSION
from custom_components.hubinet_ops.frontend import (
    FRONTEND_DIRECTORY,
    STATIC_URL,
    card_module_url,
)
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import setup_integration

pytestmark = pytest.mark.usefixtures("mock_proxmox_client")


@pytest.fixture
def frontend_loaded(hass: HomeAssistant):
    """Stand in for Home Assistant's stage-0 http and frontend integrations."""
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.config.components.add("frontend")
    with patch("custom_components.hubinet_ops.frontend.add_extra_js_url") as extra:
        yield hass.http.async_register_static_paths, extra


def test_card_url_is_versioned_per_release() -> None:
    """Each release changes the module URL, so cached old cards are not reused."""
    assert card_module_url() == (
        f"/hubinet_ops_static/hubinet-ops-cards.js?v={INTEGRATION_VERSION}"
    )


async def test_setup_serves_directory_and_registers_module_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, frontend_loaded
) -> None:
    """Delivery runs once per start, not once per entry setup or reload."""
    register, extra = frontend_loaded
    await setup_integration(hass, mock_config_entry)
    register.assert_awaited_once_with(
        [StaticPathConfig(STATIC_URL, str(FRONTEND_DIRECTORY), True)]
    )
    extra.assert_called_once_with(hass, card_module_url())

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    register.assert_awaited_once()
    extra.assert_called_once()


async def test_missing_frontend_skips_delivery_without_affecting_setup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Without http/frontend the integration still loads normally."""
    with patch("custom_components.hubinet_ops.frontend.add_extra_js_url") as extra:
        await setup_integration(hass, mock_config_entry)
    extra.assert_not_called()
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_delivery_failure_is_logged_and_isolated(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    frontend_loaded,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A static-path failure never disables native or package functionality."""
    register, extra = frontend_loaded
    register.side_effect = RuntimeError("route conflict")
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    extra.assert_not_called()
    assert "Could not deliver the Hubinet-Ops dashboard card" in caplog.text
