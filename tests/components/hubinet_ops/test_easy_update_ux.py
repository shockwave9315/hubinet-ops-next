"""Execute shipped Easy UX blueprints with the pinned Home Assistant engine."""

import asyncio
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from tests.common import MockConfigEntry, async_fire_time_changed  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.packages.models import (
    PackageScanRecord,
    PackageScanStatus,
    PackageUpdateStatus,
)
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint import (
    BLUEPRINT_SCHEMA,
    Blueprint,
    DomainBlueprints,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util, yaml as yaml_util

from . import setup_integration
from .test_packages import RESULT

ROOT = Path(__file__).resolve().parents[3]
SCAN_PATH = "automation/hubinet_ops_daily_package_scan.yaml"
UPDATE_PATH = "script/hubinet_ops_one_click_update.yaml"
PENDING = "sensor.ct_nginx_pending_package_updates"
UPDATE_STATUS = "sensor.ct_nginx_package_update"
UPDATE = "button.ct_nginx_update_packages"
UNUSED = "sensor.ct_nginx_unused_packages"
AUTOREMOVE = "button.ct_nginx_autoremove_unused_packages"
INPUTS = {
    "pending_packages_sensor": PENDING,
    "package_update_sensor": UPDATE_STATUS,
    "update_button": UPDATE,
    "unused_packages_sensor": UNUSED,
    "autoremove_button": AUTOREMOVE,
}


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> None:
    """Keep package controls enabled in the real contract test."""


@contextmanager
def _load_shipped_blueprints():
    """Use the real blueprint loader/schema with repository artifact paths."""

    def load(domain_blueprints, path):
        return Blueprint(
            yaml_util.load_yaml(ROOT / "blueprints" / domain_blueprints.domain / path),
            expected_domain=domain_blueprints.domain,
            schema=AUTOMATION_BLUEPRINT_SCHEMA
            if domain_blueprints.domain == "automation"
            else BLUEPRINT_SCHEMA,
        )

    with patch.object(DomainBlueprints, "_load_blueprint", load):
        yield


async def _setup_script(hass: HomeAssistant, *, autoremove: bool = False) -> None:
    with _load_shipped_blueprints():
        assert await async_setup_component(
            hass,
            "script",
            {
                "script": {
                    "easy_update": {
                        "use_blueprint": {
                            "path": Path(UPDATE_PATH).name,
                            "input": {**INPUTS, "autoremove_after_update": autoremove},
                        }
                    }
                }
            },
        )
    assert hass.states.get("script.easy_update") is not None


def _services(hass: HomeAssistant, *, confirmed: bool = True):
    """Observe action routing and simulate only backend-published HA facts."""
    calls = []
    now = dt_util.utcnow().isoformat()
    hass.states.async_set(PENDING, "2", {"scan_status": "success", "last_attempt": now})
    hass.states.async_set(UPDATE_STATUS, "success", {"last_attempt": "old-attempt"})
    hass.states.async_set(UNUSED, "7", {"observed_at": "old-observation"})
    hass.states.async_set(AUTOREMOVE, "unknown")  # Available, never pressed button.

    async def get_plan(call: ServiceCall):
        calls.append(("get", dict(call.data)))
        return {
            PENDING: {
                "status": "success",
                "token": "exact-scan-token",
                "packages": [{"name": "example"}],
            }
        }

    async def confirm(call: ServiceCall):
        calls.append(("confirm", dict(call.data)))
        return {PENDING: {"reviewed": confirmed}}

    async def press(call: ServiceCall):
        calls.append(("press", dict(call.data)))
        if call.data["entity_id"] == [UPDATE]:
            hass.states.async_set(UPDATE_STATUS, "running", {"last_attempt": now})
            hass.states.async_set(UNUSED, "unknown")

    hass.services.async_register(
        DOMAIN, "get_package_plan", get_plan, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN,
        "confirm_package_review",
        confirm,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("button", "press", press)
    return calls, now


async def _run(hass: HomeAssistant) -> None:
    await hass.services.async_call("script", "easy_update", blocking=True)


def _presses(calls):
    return [data["entity_id"] for kind, data in calls if kind == "press"]


@pytest.mark.parametrize("count", ["0", "unknown", "unavailable"])
async def test_easy_update_no_current_plan_does_not_mutate(
    hass: HomeAssistant, count: str
) -> None:
    """Unknown and empty evidence never reaches confirmation or mutation."""
    calls, _ = _services(hass)
    hass.states.async_set(PENDING, count, {"scan_status": "success"})
    await _setup_script(hass)
    await _run(hass)
    assert calls == []


@pytest.mark.parametrize("confirmed", [False, True])
async def test_easy_update_exact_response_token_and_confirmation(
    hass: HomeAssistant, confirmed: bool
) -> None:
    """Only the exact per-entity returned token and positive response authorize Update."""
    calls, _ = _services(hass, confirmed=confirmed)
    await _setup_script(hass)
    await _run(hass)
    assert calls[0] == ("get", {"entity_id": [PENDING]})
    assert calls[1] == (
        "confirm",
        {"entity_id": [PENDING], "token": "exact-scan-token"},
    )
    assert _presses(calls) == ([[UPDATE]] if confirmed else [])


@pytest.mark.parametrize(
    ("status", "count", "fresh", "expected"),
    [
        ("failed", "4", True, False),
        ("unknown", "4", True, False),
        ("unavailable", "4", True, False),
        ("success", "0", True, False),
        ("success", "unknown", True, False),
        ("success", "unavailable", True, False),
        ("success", "4", False, False),
        ("success", "4", True, True),
    ],
)
async def test_easy_update_optional_cleanup_requires_fresh_terminal_success(
    hass: HomeAssistant, status: str, count: str, fresh: bool, expected: bool
) -> None:
    """Optional cleanup observes the new Update, never old or uncertain evidence."""
    calls, attempt = _services(hass)
    await _setup_script(hass, autoremove=True)
    run = asyncio.create_task(_run(hass))
    await hass.async_block_till_done(wait_background_tasks=False)
    assert _presses(calls) == [[UPDATE]]
    assert not run.done()
    hass.states.async_set(
        UNUSED,
        count,
        {"observed_at": attempt if fresh else "2000-01-01T00:00:00+00:00"},
    )
    hass.states.async_set(UPDATE_STATUS, status, {"last_attempt": attempt})
    await run
    assert _presses(calls) == [[UPDATE]] + ([[AUTOREMOVE]] if expected else [])


async def test_easy_update_waits_for_health_owned_button_availability(
    hass: HomeAssistant,
) -> None:
    """Automatic Health may keep Autoremove unavailable after Update succeeds."""
    calls, attempt = _services(hass)
    hass.states.async_set(AUTOREMOVE, "unavailable")
    await _setup_script(hass, autoremove=True)
    run = asyncio.create_task(_run(hass))
    await hass.async_block_till_done(wait_background_tasks=False)
    hass.states.async_set(UNUSED, "4", {"observed_at": attempt})
    hass.states.async_set(UPDATE_STATUS, "success", {"last_attempt": attempt})
    await hass.async_block_till_done(wait_background_tasks=False)
    assert _presses(calls) == [[UPDATE]]
    assert not run.done()
    hass.states.async_set(AUTOREMOVE, "unknown")
    await run
    assert _presses(calls) == [[UPDATE], [AUTOREMOVE]]


async def test_easy_update_timeout_does_not_touch_backend(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The one-hour wait stops YAML alone and leaves backend RUNNING facts intact."""
    calls, attempt = _services(hass)
    await _setup_script(hass, autoremove=True)
    run = asyncio.create_task(_run(hass))
    await hass.async_block_till_done(wait_background_tasks=False)
    freezer.tick(timedelta(hours=1, seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await run
    assert _presses(calls) == [[UPDATE]]
    assert hass.states.get(UPDATE_STATUS).state == "running"
    assert hass.states.get(UPDATE_STATUS).attributes["last_attempt"] == attempt


async def test_easy_update_real_entity_action_contract(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Prove actual service names, per-entity responses, and backend acceptance."""
    await setup_integration(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    manager._set_record(  # noqa: SLF001
        "pve1",
        200,
        PackageScanRecord(
            status=PackageScanStatus.SUCCESS,
            result=RESULT,
            token="real-token",
            last_attempt=dt_util.utcnow(),
        ),
    )
    await _setup_script(hass)
    # Stop at the ownership boundary: acceptance is real, remote lifecycle is not run.
    with (
        patch.object(
            manager, "confirm_review", wraps=manager.confirm_review
        ) as confirm,
        patch.object(manager, "_async_run_update") as lifecycle,
    ):
        await _run(hass)
    confirm.assert_called_once_with("pve1", 200, "real-token")
    lifecycle.assert_awaited_once()
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.RUNNING


async def test_easy_update_changed_scan_during_plan_read_requires_new_click(
    hass: HomeAssistant,
) -> None:
    """A later Scan may not replace the displayed plan within one script click."""
    calls, _ = _services(hass)

    async def changed_plan(call):
        calls.append(("get", dict(call.data)))
        hass.states.async_set(
            PENDING, "2", {"scan_status": "success", "last_attempt": "new-scan"}
        )
        return {
            PENDING: {"status": "success", "token": "replacement", "packages": [{}]}
        }

    hass.services.async_register(
        DOMAIN,
        "get_package_plan",
        changed_plan,
        supports_response=SupportsResponse.ONLY,
    )
    await _setup_script(hass)
    await _run(hass)
    assert [kind for kind, _data in calls] == ["get"]


@pytest.mark.parametrize("status", ["never", "failed", "success"])
async def test_easy_update_empty_or_unsuccessful_action_plan_stops(
    hass: HomeAssistant, status: str
) -> None:
    """Even a positive sensor count cannot authorize an empty/failed action plan."""
    calls, _ = _services(hass)

    async def empty_plan(call):
        calls.append(("get", dict(call.data)))
        return {PENDING: {"status": status, "token": "unused", "packages": []}}

    hass.services.async_register(
        DOMAIN, "get_package_plan", empty_plan, supports_response=SupportsResponse.ONLY
    )
    await _setup_script(hass)
    await _run(hass)
    assert [kind for kind, _data in calls] == ["get"]


async def test_easy_update_interruption_never_starts_autoremove(
    hass: HomeAssistant,
) -> None:
    """Stopping the script leaves its already-started backend attempt untouched."""
    calls, attempt = _services(hass)
    await _setup_script(hass, autoremove=True)
    run = asyncio.create_task(_run(hass))
    await hass.async_block_till_done(wait_background_tasks=False)
    await hass.services.async_call(
        "script", "turn_off", {"entity_id": "script.easy_update"}, blocking=True
    )
    await run
    assert _presses(calls) == [[UPDATE]]
    assert hass.states.get(UPDATE_STATUS).state == "running"
    assert hass.states.get(UPDATE_STATUS).attributes["last_attempt"] == attempt


async def test_easy_update_cleanup_wait_uses_remaining_hour(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Health/button availability cannot extend the initial observation deadline."""
    calls, attempt = _services(hass)
    hass.states.async_set(AUTOREMOVE, "unavailable")
    await _setup_script(hass, autoremove=True)
    run = asyncio.create_task(_run(hass))
    await hass.async_block_till_done(wait_background_tasks=False)
    freezer.tick(timedelta(minutes=59))
    hass.states.async_set(UNUSED, "4", {"observed_at": attempt})
    hass.states.async_set(UPDATE_STATUS, "success", {"last_attempt": attempt})
    await hass.async_block_till_done(wait_background_tasks=False)
    assert not run.done()
    freezer.tick(timedelta(minutes=1, seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await run
    assert _presses(calls) == [[UPDATE]]
    assert hass.states.get(UPDATE_STATUS).state == "success"


def test_easy_ux_artifacts_only_call_existing_ha_actions() -> None:
    """Blueprints contain no direct snapshot/helper/package mutation entry points."""
    scan = yaml_util.load_yaml(ROOT / "blueprints" / SCAN_PATH)
    update = yaml_util.load_yaml(ROOT / "blueprints" / UPDATE_PATH)

    def actions(value):
        if isinstance(value, dict):
            if isinstance(value.get("action"), str):
                yield value["action"]
            for child in value.values():
                yield from actions(child)
        elif isinstance(value, list):
            for child in value:
                yield from actions(child)

    assert set(actions(scan)) == {"button.press"}
    assert set(actions(update)) == {
        "hubinet_ops.get_package_plan",
        "hubinet_ops.confirm_package_review",
        "button.press",
    }
    assert scan["blueprint"]["input"]["startup_delay"]["default"] == 60
    assert scan["blueprint"]["input"]["scan_after_start"]["default"] is True
    assert update["blueprint"]["input"]["autoremove_after_update"]["default"] is False


@pytest.mark.parametrize("started", [False, True])
async def test_easy_update_old_success_and_fast_new_success(
    hass: HomeAssistant, started: bool
) -> None:
    """A fast new SUCCESS works; a historical SUCCESS cannot authorize cleanup."""
    calls, attempt = _services(hass)

    async def press(call):
        calls.append(("press", dict(call.data)))
        if call.data["entity_id"] == [UPDATE] and started:
            hass.states.async_set(UPDATE_STATUS, "success", {"last_attempt": attempt})
            hass.states.async_set(UNUSED, "4", {"observed_at": attempt})

    hass.services.async_register("button", "press", press)
    await _setup_script(hass, autoremove=True)
    await _run(hass)
    assert _presses(calls) == [[UPDATE]] + ([[AUTOREMOVE]] if started else [])


@pytest.mark.parametrize("trigger", ["daily", "startup", "disabled"])
async def test_easy_scan_real_daily_and_startup_triggers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, trigger: str
) -> None:
    """Both real triggers target only chosen buttons and isolate target rejection."""
    buttons = ["button.first_scan", "button.broken_scan", "button.last_scan"]
    calls = []

    async def press(call):
        calls.append(call.data["entity_id"])
        if call.data["entity_id"] == [buttons[1]]:
            raise HomeAssistantError("broken target")

    hass.services.async_register("button", "press", press)
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-09-26T03:58:00+00:00")
    with _load_shipped_blueprints():
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "alias": "daily scans",
                    "use_blueprint": {
                        "path": Path(SCAN_PATH).name,
                        "input": {
                            "scan_buttons": buttons,
                            "scan_after_start": trigger != "disabled",
                            "startup_delay": 0,
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
    await hass.async_block_till_done()
    assert calls == ([] if trigger == "disabled" else [[button] for button in buttons])


@pytest.mark.parametrize(
    ("count", "update", "health", "color", "text"),
    [
        ("0", "never", "unknown", "green", "System aktualny"),
        ("20", "never", "healthy", "amber", "20 aktualizacji • 3 security"),
        ("unknown", "running", "unknown", "blue", "Aktualizacja w toku"),
        ("20", "failed", "unknown", "red", "nie powiodła"),
        ("0", "success", "failed", "red", "Health"),
        ("unknown", "never", "unknown", "grey", "Brak aktualnego skanu"),
    ],
)
async def test_easy_update_mushroom_example_uses_existing_facts(
    hass: HomeAssistant, count: str, update: str, health: str, color: str, text: str
) -> None:
    """The parsed card renders native facts and routes tap/hold to existing actions."""
    card = yaml_util.load_yaml(ROOT / "examples/dashboard/mushroom_easy_update.yaml")
    hass.states.async_set(
        card["entity"], count, {"scan_status": "success", "security_updates": 3}
    )
    hass.states.async_set("sensor.ct_nextcloud_package_update", update)
    hass.states.async_set("sensor.ct_nextcloud_health", health)
    assert Template(card["color"], hass).async_render().strip() == color
    assert text in Template(card["secondary"], hass).async_render()
    assert card["tap_action"]["perform_action"] == "script.turn_on"
    assert card["hold_action"]["perform_action"] == "button.press"
