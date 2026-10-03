"""Execute the shipped Scan blueprint with the pinned Home Assistant engine."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from freezegun.api import FrozenDateTimeFactory
import pytest
from tests.common import async_fire_time_changed  # noqa: TID251

from custom_components.hubinet_ops.blueprint_delivery import async_provision_blueprints
from custom_components.hubinet_ops.const import DOMAIN
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util, yaml as yaml_util

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "custom_components" / DOMAIN / "blueprints"
SCAN_PATH = "automation/hubinet_ops_daily_package_scan.yaml"


@asynccontextmanager
async def _load_shipped_blueprints(hass: HomeAssistant):
    """Provision shipped bytes, then use the unpatched native HA loader."""
    await async_provision_blueprints(hass)
    yield


def test_scan_blueprint_only_calls_scan_all() -> None:
    """The only shipped blueprint calls Scan All and needs no entity input."""
    scan = yaml_util.load_yaml(SOURCE / SCAN_PATH)

    def actions(value):
        if isinstance(value, dict):
            if isinstance(value.get("action"), str):
                yield value["action"]
            for child in value.values():
                yield from actions(child)
        elif isinstance(value, list):
            for child in value:
                yield from actions(child)

    assert set(actions(scan)) == {"hubinet_ops.scan_all_packages"}
    assert "scan_buttons" not in scan["blueprint"]["input"]
    assert "entity:" not in (SOURCE / SCAN_PATH).read_text()
    assert set(scan["blueprint"]["input"]) == {
        "daily_scan_time",
        "scan_after_start",
        "startup_delay",
    }
    assert scan["blueprint"]["name"] == "Hubinet-Ops — automatyczny skan aktualizacji"
    assert "Skan niczego nie aktualizuje" in scan["blueprint"]["description"]
    assert scan["blueprint"]["input"]["startup_delay"]["default"] == 60
    assert scan["blueprint"]["input"]["scan_after_start"]["default"] is True
    assert not (SOURCE / "script").exists()


@pytest.mark.parametrize("trigger", ["daily", "startup", "disabled"])
async def test_easy_scan_real_daily_and_startup_triggers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, trigger: str
) -> None:
    """Daily/startup triggers call targetless Scan All; disabled startup does not."""
    calls = []

    async def scan_all(call):
        calls.append(dict(call.data))

    hass.services.async_register(DOMAIN, "scan_all_packages", scan_all)
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-09-26T03:58:00+00:00")
    async with _load_shipped_blueprints(hass):
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "alias": "daily scans",
                    "use_blueprint": {
                        "path": f"hubinet_ops/{Path(SCAN_PATH).name}",
                        "input": {
                            "scan_after_start": trigger != "disabled",
                            "startup_delay": 5,
                        },
                    },
                }
            },
        )
    await hass.async_block_till_done()
    if trigger == "daily":
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(minutes=2, seconds=1)
        )
    else:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        # Let the real script enter its delay before advancing HA's clock.
        for _ in range(10):
            await asyncio.sleep(0)
        assert calls == []
        freezer.tick(timedelta(seconds=4))
        async_fire_time_changed(hass, dt_util.utcnow())
        for _ in range(10):
            await asyncio.sleep(0)
        assert calls == []
        freezer.tick(timedelta(seconds=2))
        async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert calls == ([] if trigger == "disabled" else [{}])
