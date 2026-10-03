"""Test managed blueprint delivery through actual HA configuration paths."""

# ruff: noqa: SLF001 -- verify existing ephemeral state and fixed shipped sources

import logging
from pathlib import Path
import shutil
import threading
from unittest.mock import patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops import blueprint_delivery as delivery
from homeassistant.components.automation.helpers import (
    async_get_blueprints as automation_blueprints,
)
from homeassistant.components.blueprint import Blueprint
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import setup_integration


async def test_blueprint_delivery_creates_exact_files_idempotently(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Ship the Scan file, leave unrelated copies alone, never rewrite equal bytes."""
    hass.config.config_dir = str(tmp_path)
    unrelated = tmp_path / "blueprints/automation/user/own.yaml"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"user-owned")
    same_namespace = tmp_path / "blueprints/script/hubinet_ops/other.yaml"
    same_namespace.parent.mkdir(parents=True)
    same_namespace.write_bytes(b"also-user-owned")
    with patch.object(
        delivery, "write_utf8_file", wraps=delivery.write_utf8_file
    ) as write:
        await delivery.async_provision_blueprints(hass)
        assert write.call_count == 1
        stamps = {}
        for domain, filename in delivery._FILES:
            destination = tmp_path / "blueprints" / domain / "hubinet_ops" / filename
            assert (
                destination.read_bytes()
                == (delivery._SOURCE / domain / filename).read_bytes()
            )
            stamps[destination] = destination.stat().st_mtime_ns
        write.reset_mock()
        await delivery.async_provision_blueprints(hass)
        write.assert_not_called()
    assert all(path.stat().st_mtime_ns == stamp for path, stamp in stamps.items())
    assert unrelated.read_bytes() == b"user-owned"
    assert same_namespace.read_bytes() == b"also-user-owned"
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == {
        *stamps,
        unrelated,
        same_namespace,
    }


async def test_blueprint_delivery_native_discovery_and_changed_source(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """The automation manager discovers provisioned YAML and drops old content."""
    hass.config.config_dir = str(tmp_path / "config")
    shipped = tmp_path / "shipped"
    shutil.copytree(delivery._SOURCE, shipped)
    with patch.object(delivery, "_SOURCE", shipped):
        await delivery.async_provision_blueprints(hass)
        for domain, manager in (("automation", automation_blueprints(hass)),):
            filename = next(name for kind, name in delivery._FILES if kind == domain)
            path = f"hubinet_ops/{filename}"
            found = await manager.async_get_blueprints()
            assert isinstance(found[path], Blueprint)
            assert found[path].domain == domain
            source = shipped / domain / filename
            source.write_text(
                source.read_text().replace("Hubinet-Ops —", "Nowa wersja —")
            )
            await delivery.async_provision_blueprints(hass)
            reloaded = await manager.async_get_blueprint(path)
            assert reloaded.name.startswith("Nowa wersja —")
            assert (
                Path(hass.config.path("blueprints", domain, path)).read_bytes()
                == source.read_bytes()
            )


async def test_blueprint_delivery_io_is_off_loop(hass: HomeAssistant) -> None:
    """All filesystem synchronization happens in HA's executor."""
    loop_thread = threading.get_ident()
    threads = []

    def provision(config_dir):
        threads.append(threading.get_ident())
        return set()

    with patch.object(delivery, "_provision", provision):
        await delivery.async_provision_blueprints(hass)
    assert len(threads) == 1 and threads[0] != loop_thread


@pytest.mark.usefixtures("mock_proxmox_client")
async def test_blueprint_delivery_failure_preserves_native_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken delivery source logs failure but native Proxmox still loads."""
    with patch.object(delivery, "_SOURCE", tmp_path / "missing"):
        await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.pve1_status") is not None
    assert "Could not provision Hubinet-Ops blueprint" in caplog.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_blueprint_delivery_failed_replace_preserves_previous_file(
    hass: HomeAssistant, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Replacement failure retains the old file and logs it."""
    hass.config.config_dir = str(tmp_path)
    destination = (
        tmp_path
        / "blueprints/automation/hubinet_ops/hubinet_ops_daily_package_scan.yaml"
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous-copy")
    from homeassistant.util import file as file_util  # noqa: PLC0415

    original_replace = file_util.os.replace

    def replace(source, target):
        if target == str(destination):
            raise OSError("replacement failed")
        original_replace(source, target)

    with patch.object(file_util.os, "replace", replace):
        await delivery.async_provision_blueprints(hass)
    assert destination.read_bytes() == b"previous-copy"
    assert "Could not provision" in caplog.text
    assert list(destination.parent.iterdir()) == [destination]

