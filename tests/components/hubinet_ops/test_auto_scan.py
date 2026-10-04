"""Daily automatic Scan configured in the integration options."""

from datetime import datetime
from types import MappingProxyType
from unittest.mock import MagicMock, call, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from tests.common import MockConfigEntry, async_fire_time_changed  # noqa: TID251

from custom_components.hubinet_ops.auto_scan import CONF_AUTO_SCAN, CONF_AUTO_SCAN_TIME
from custom_components.hubinet_ops.const import CONF_VM_GUEST_MEMORY
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
    assert defaults == {
        CONF_AUTO_SCAN: False,
        CONF_AUTO_SCAN_TIME: "06:00:00",
        CONF_VM_GUEST_MEMORY: False,
    }
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
        CONF_VM_GUEST_MEMORY: False,
    }
    reload.assert_called_once_with(mock_config_entry.entry_id)


async def _save_options(
    hass: HomeAssistant, entry: MockConfigEntry, **changes: object
) -> MagicMock:
    """Submit the options form with its shown defaults plus the given changes."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    shown = {key.schema: key.default() for key in result["data_schema"].schema}
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**shown, **changes}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return reload


async def test_vm_guest_memory_defaults_off(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Without the option the entry keeps the lightweight QEMU listing."""
    await setup_integration(hass, mock_config_entry)
    assert CONF_VM_GUEST_MEMORY not in mock_config_entry.options
    assert mock_config_entry.runtime_data.vm_guest_memory is False
    listing = mock_proxmox_client._node_mock.qemu.get  # noqa: SLF001
    assert listing.call_args_list
    assert all(used == call() for used in listing.call_args_list)


async def test_vm_guest_memory_saved_on_and_off_reloads_and_is_read(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Saving reloads; the new coordinator reads the option; others are kept."""
    _set_options(mock_config_entry, auto_scan=True, auto_scan_time="05:30:00")
    await setup_integration(hass, mock_config_entry)
    listing = mock_proxmox_client._node_mock.qemu.get  # noqa: SLF001
    first = mock_config_entry.runtime_data
    assert first.vm_guest_memory is False

    reload = await _save_options(
        hass, mock_config_entry, **{CONF_VM_GUEST_MEMORY: True}
    )
    reload.assert_called_once_with(mock_config_entry.entry_id)
    assert mock_config_entry.options == {
        CONF_AUTO_SCAN: True,
        CONF_AUTO_SCAN_TIME: "05:30:00",
        CONF_VM_GUEST_MEMORY: True,
    }
    assert mock_config_entry.state is ConfigEntryState.LOADED
    second = mock_config_entry.runtime_data
    assert second is not first
    assert second.vm_guest_memory is True
    assert listing.call_args_list[-1] == call(full=1)

    reload = await _save_options(
        hass, mock_config_entry, **{CONF_VM_GUEST_MEMORY: False}
    )
    reload.assert_called_once_with(mock_config_entry.entry_id)
    assert mock_config_entry.options == {
        CONF_AUTO_SCAN: True,
        CONF_AUTO_SCAN_TIME: "05:30:00",
        CONF_VM_GUEST_MEMORY: False,
    }
    third = mock_config_entry.runtime_data
    assert third is not second
    assert third.vm_guest_memory is False
    assert listing.call_args_list[-1] == call()


async def test_changing_scan_options_keeps_vm_guest_memory(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The form shows the saved value, so other changes do not reset it."""
    _set_options(mock_config_entry, **{CONF_VM_GUEST_MEMORY: True})
    await setup_integration(hass, mock_config_entry)
    await _save_options(
        hass, mock_config_entry, auto_scan=True, auto_scan_time="07:15:00"
    )
    assert mock_config_entry.options == {
        CONF_AUTO_SCAN: True,
        CONF_AUTO_SCAN_TIME: "07:15:00",
        CONF_VM_GUEST_MEMORY: True,
    }
    assert mock_config_entry.runtime_data.vm_guest_memory is True


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
