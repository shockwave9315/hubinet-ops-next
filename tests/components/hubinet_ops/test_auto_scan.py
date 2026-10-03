"""Daily automatic Scan configured in the integration options."""

from datetime import datetime
from types import MappingProxyType
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from tests.common import MockConfigEntry, async_fire_time_changed  # noqa: TID251

from custom_components.hubinet_ops.auto_scan import CONF_AUTO_SCAN, CONF_AUTO_SCAN_TIME
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from . import setup_integration

pytestmark = pytest.mark.usefixtures("mock_proxmox_client")


def _set_options(entry: MockConfigEntry, **options) -> None:
    object.__setattr__(entry, "options", MappingProxyType(options))


async def _at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: str) -> None:
    freezer.move_to(datetime.fromisoformat(when))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_options_flow_stores_choice_and_reloads(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Defaults are off at 06:00; saving stores the choice and reloads."""
    await setup_integration(hass, mock_config_entry)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults == {CONF_AUTO_SCAN: False, CONF_AUTO_SCAN_TIME: "06:00:00"}
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_AUTO_SCAN: True, CONF_AUTO_SCAN_TIME: "05:30:00"},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_AUTO_SCAN: True,
        CONF_AUTO_SCAN_TIME: "05:30:00",
    }
    reload.assert_called_once_with(mock_config_entry.entry_id)


async def test_enabled_scan_runs_daily_at_local_time(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The entry is scanned once at the chosen local time, every day."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    freezer.move_to("2026-10-03T05:00:00+02:00")
    _set_options(mock_config_entry, auto_scan=True, auto_scan_time="06:00:00")
    with patch("custom_components.hubinet_ops.auto_scan.async_scan_entry") as scan:
        await setup_integration(hass, mock_config_entry)
        await _at(hass, freezer, "2026-10-03T05:59:59+02:00")
        scan.assert_not_called()
        await _at(hass, freezer, "2026-10-03T06:00:00+02:00")
        scan.assert_called_once_with(mock_config_entry)
        await _at(hass, freezer, "2026-10-04T06:00:00+02:00")
        assert scan.call_count == 2


async def test_disabled_scan_never_runs(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Off (the default) registers nothing."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-10-03T05:00:00+00:00")
    with patch("custom_components.hubinet_ops.auto_scan.async_scan_entry") as scan:
        await setup_integration(hass, mock_config_entry)
        await _at(hass, freezer, "2026-10-03T06:00:00+00:00")
    scan.assert_not_called()


async def test_unload_removes_the_daily_scan(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After unload the time trigger is gone."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-10-03T05:00:00+00:00")
    _set_options(mock_config_entry, auto_scan=True, auto_scan_time="06:00:00")
    with patch("custom_components.hubinet_ops.auto_scan.async_scan_entry") as scan:
        await setup_integration(hass, mock_config_entry)
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
        await _at(hass, freezer, "2026-10-03T06:00:00+00:00")
    scan.assert_not_called()


async def test_daily_scan_uses_the_existing_manager_entry_point(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Running package LXCs are scanned; stopped ones are skipped by the manager."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-10-03T05:00:00+00:00")
    _set_options(mock_config_entry, auto_scan=True, auto_scan_time="06:00:00")
    await setup_integration(hass, mock_config_entry)
    # Let HA's delayed reload for re-enabled entities finish first.
    await _at(hass, freezer, "2026-10-03T05:01:00+00:00")
    manager = mock_config_entry.runtime_data.package_manager
    with patch.object(manager, "async_start_scan") as start:
        await _at(hass, freezer, "2026-10-03T06:00:00+00:00")
    assert sorted(
        (call.args, call.kwargs["target_is_running"]) for call in start.call_args_list
    ) == [(("pve1", 200), True), (("pve1", 201), False)]
