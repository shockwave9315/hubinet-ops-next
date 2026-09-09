"""Test the Proxmox VE component diagnostics."""

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from syrupy.assertion import SnapshotAssertion
from syrupy.filters import props
from tests.common import MockConfigEntry
from tests.components.diagnostics import get_diagnostics_for_config_entry
from tests.typing import ClientSessionGenerator

from custom_components.hubinet_ops.const import (
    CONF_SSH_HOST_KEY,
    CONF_SSH_PRIVATE_KEY,
    CONF_TOKEN_SECRET,
)

from . import setup_integration


async def test_get_config_entry_diagnostics(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test if get_config_entry_diagnostics returns the correct data."""
    await setup_integration(hass, mock_config_entry)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_TOKEN_SECRET: "api-secret",
            CONF_SSH_PRIVATE_KEY: "private-secret",
            CONF_SSH_HOST_KEY: "host-key-material",
            "enrollment": "HUBINET1-raw-secret",
        },
    )

    diagnostics_entry = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )
    assert diagnostics_entry == snapshot(
        exclude=props(
            "created_at",
            "modified_at",
        ),
    )
    diagnostic_data = diagnostics_entry["config_entry"]["data"]
    assert diagnostic_data[CONF_TOKEN_SECRET] == "**REDACTED**"
    assert diagnostic_data[CONF_SSH_PRIVATE_KEY] == "**REDACTED**"
    assert diagnostic_data[CONF_SSH_HOST_KEY] == "**REDACTED**"
    assert diagnostic_data["enrollment"] == "**REDACTED**"
