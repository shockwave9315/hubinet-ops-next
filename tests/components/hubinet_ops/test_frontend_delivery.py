"""The dashboard card ships with the integration without a manual resource."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.button import (
    CONTAINER_BUTTONS,
    PACKAGE_SCAN_BUTTON,
    PACKAGE_UPDATE_BUTTON,
    VM_BUTTONS,
)
from custom_components.hubinet_ops.const import INTEGRATION_VERSION
from custom_components.hubinet_ops.frontend import (
    CARD_MODULE,
    FRONTEND_DIRECTORY,
    STATIC_URL,
    async_register_frontend,
    card_module_url,
)
from custom_components.hubinet_ops.select import SNAPSHOT_SELECT
from custom_components.hubinet_ops.sensor import (
    CONTAINER_SENSORS,
    PACKAGE_HEALTH_SENSOR,
    PACKAGE_SCAN_SENSOR,
    PACKAGE_UPDATE_SENSOR,
    UNUSED_PACKAGES_SENSOR,
    VM_SENSORS,
)
from custom_components.hubinet_ops.snapshot_restore import (
    SNAPSHOT_DELETE_BUTTON,
    SNAPSHOT_RESTORE_BUTTON,
)
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import LovelaceData
from homeassistant.components.lovelace.const import (
    LOVELACE_DATA,
    MODE_STORAGE,
    MODE_YAML,
)
from homeassistant.components.lovelace.dashboard import LovelaceStorage
from homeassistant.components.lovelace.resources import (
    ResourceStorageCollection,
    ResourceYAMLCollection,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import setup_integration

pytestmark = pytest.mark.usefixtures("mock_proxmox_client")


@pytest.fixture
def frontend_loaded(hass: HomeAssistant):
    """Use HA's real resource collection with a mocked static-path server."""
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.config.components.update(("frontend", "lovelace"))
    dashboard = LovelaceStorage(hass, None)
    resources = ResourceStorageCollection(hass, dashboard)
    hass.data[LOVELACE_DATA] = LovelaceData(
        resource_mode=MODE_STORAGE,
        dashboards={None: dashboard},
        resources=resources,
        yaml_dashboards={},
    )
    return hass.http.async_register_static_paths, resources


def test_card_url_is_versioned_per_release() -> None:
    """Each release changes the module URL, so cached old cards are not reused."""
    assert card_module_url() == (
        f"/hubinet_ops_static/hubinet-ops-cards.js?v={INTEGRATION_VERSION}"
    )


async def test_setup_serves_directory_and_registers_module_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, frontend_loaded
) -> None:
    """Delivery runs once per start, not once per entry setup or reload."""
    register, resources = frontend_loaded
    with patch("homeassistant.components.frontend.add_extra_js_url") as extra:
        await setup_integration(hass, mock_config_entry)
    extra.assert_not_called()
    register.assert_awaited_once_with(
        [StaticPathConfig(STATIC_URL, str(FRONTEND_DIRECTORY), True)]
    )
    items = list(resources.async_items())
    assert len(items) == 1
    assert items[0]["url"] == card_module_url()
    assert items[0]["type"] == "module"

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    register.assert_awaited_once()
    assert list(resources.async_items()) == items


async def test_missing_frontend_skips_delivery_without_affecting_setup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Without http/frontend the integration still loads normally."""
    with patch("homeassistant.components.frontend.add_extra_js_url") as extra:
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
    register, resources = frontend_loaded
    register.side_effect = RuntimeError("route conflict")
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert not list(resources.async_items())
    assert "Could not deliver the Hubinet-Ops dashboard card" in caplog.text


async def test_missing_lovelace_skips_delivery(hass: HomeAssistant, frontend_loaded):
    """HTTP/frontend alone must not load a card through index HTML."""
    register, _ = frontend_loaded
    del hass.data[LOVELACE_DATA]
    await async_register_frontend(hass)
    register.assert_not_awaited()


async def test_resource_upgrade_preserves_id_and_unrelated_resources(
    hass: HomeAssistant, frontend_loaded
) -> None:
    """Use the native collection to upgrade one relative module, not dashboards."""
    _, resources = frontend_loaded
    old = await resources.async_create_item(
        {"url": f"{STATIC_URL}/{CARD_MODULE}?v=older", "res_type": "js"}
    )
    unrelated = [
        await resources.async_create_item({"url": url, "res_type": "module"})
        for url in (
            "/hacsfiles/another-card/another-card.js",
            f"https://other.example{STATIC_URL}/{CARD_MODULE}?v=other",
            f"{STATIC_URL}/{CARD_MODULE}.backup?v=other",
        )
    ]
    await async_register_frontend(hass)
    assert list(resources.async_items()) == [
        {"id": old["id"], "url": card_module_url(), "type": "module"},
        *unrelated,
    ]


async def test_duplicate_owned_resources_are_consolidated(
    hass: HomeAssistant, frontend_loaded
) -> None:
    """Old versions of this exact relative path must not load multiple copies."""
    _, resources = frontend_loaded
    first = await resources.async_create_item(
        {"url": card_module_url(), "res_type": "module"}
    )
    await resources.async_create_item(
        {"url": f"{STATIC_URL}/{CARD_MODULE}?v=previous", "res_type": "module"}
    )
    await async_register_frontend(hass)
    assert list(resources.async_items()) == [first]


async def test_current_resource_needs_no_storage_mutation(
    hass: HomeAssistant, frontend_loaded
) -> None:
    """A restart with the current module should only load the native collection."""
    _, resources = frontend_loaded
    item = await resources.async_create_item(
        {"url": card_module_url(), "res_type": "module"}
    )
    with (
        patch.object(
            resources, "async_create_item", wraps=resources.async_create_item
        ) as create,
        patch.object(
            resources, "async_update_item", wraps=resources.async_update_item
        ) as update,
        patch.object(
            resources, "async_delete_item", wraps=resources.async_delete_item
        ) as delete,
    ):
        await async_register_frontend(hass)
    create.assert_not_called()
    update.assert_not_called()
    delete.assert_not_called()
    assert list(resources.async_items()) == [item]


async def test_resource_failure_is_logged_and_isolated(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    frontend_loaded,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A native resource save failure must not disable the Proxmox integration."""
    _, resources = frontend_loaded
    with patch.object(
        resources, "async_create_item", side_effect=OSError("save failed")
    ):
        await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert not list(resources.async_items())
    assert "Could not deliver the Hubinet-Ops dashboard card" in caplog.text


@pytest.mark.parametrize("configured", [False, True])
async def test_yaml_resources_remain_operator_configured(
    hass: HomeAssistant,
    frontend_loaded,
    caplog: pytest.LogCaptureFixture,
    configured: bool,
) -> None:
    """Use native YAML declarations without silently overlaying the user's list."""
    register, _ = frontend_loaded
    items = [{"url": "/another-card.js", "type": "module"}]
    if configured:
        items.append({"url": card_module_url(), "type": "module"})
    resources = ResourceYAMLCollection(items.copy())
    hass.data[LOVELACE_DATA].resource_mode = MODE_YAML
    hass.data[LOVELACE_DATA].resources = resources
    await async_register_frontend(hass)
    register.assert_awaited_once()
    assert resources.async_items() == items
    assert ("Lovelace uses YAML resources" in caplog.text) is not configured


def test_card_assets_ship_inside_the_integration_package() -> None:
    """HACS installs only custom_components/hubinet_ops, so assets live there."""
    card = (FRONTEND_DIRECTORY / CARD_MODULE).read_text(encoding="utf-8")
    logic = (FRONTEND_DIRECTORY / "easy-update-logic.js").read_text(encoding="utf-8")
    assert 'CARD_TYPE = "hubinet-ops-easy-update-card"' in logic
    assert 'name: "Hubinet-Ops Easy Update"' in card
    assert "window.customCards" in card
    assert "static getConfigForm()" in card
    # The logic module is fetched with the card's own release query.
    assert "new URL(import.meta.url).search" in card
    # No remote code or bare package imports; dependency-free modules only.
    for source in (card, logic):
        assert "http://" not in source
        assert 'from "' not in source.replace('from "../', "")
        assert "unpkg" not in source
        assert "cdn" not in source


def test_card_roles_match_integration_translation_keys() -> None:
    """The card resolves exactly the keys the integration's entities define."""
    logic = (FRONTEND_DIRECTORY / "easy-update-logic.js").read_text(encoding="utf-8")
    expected = {
        ("sensor", PACKAGE_SCAN_SENSOR.translation_key),
        ("sensor", PACKAGE_UPDATE_SENSOR.translation_key),
        ("sensor", UNUSED_PACKAGES_SENSOR.translation_key),
        ("sensor", PACKAGE_HEALTH_SENSOR.translation_key),
        ("button", PACKAGE_SCAN_BUTTON.translation_key),
        # Eligibility: the same button the picker selects by its update class.
        ("button", PACKAGE_UPDATE_BUTTON.translation_key),
        (
            "sensor",
            next(
                description.translation_key
                for description in CONTAINER_SENSORS
                if description.key == "container_status"
            ),
        ),
    }
    for domain, key in expected:
        assert f'["{domain}", "{key}"]' in logic
    # The picker offers only devices with the package Update button.
    card = (FRONTEND_DIRECTORY / CARD_MODULE).read_text(encoding="utf-8")
    assert PACKAGE_UPDATE_BUTTON.device_class == "update"
    assert 'domain: "button",\n                device_class: "update",' in card
    assert "supported_features" not in card


def test_guest_cards_ship_with_the_easy_update_card() -> None:
    """The guest cards load from the delivered module with its release query."""
    card = (FRONTEND_DIRECTORY / CARD_MODULE).read_text(encoding="utf-8")
    guest_cards = (FRONTEND_DIRECTORY / "hubinet-ops-guest-cards.js").read_text(
        encoding="utf-8"
    )
    guest_logic = (FRONTEND_DIRECTORY / "guest-card-logic.js").read_text(
        encoding="utf-8"
    )
    assert "./hubinet-ops-guest-cards.js${new URL(import.meta.url).search}" in card
    # Nothing is defined before Home Assistant's app (and its registry polyfill).
    wait = card.index('customElements.whenDefined("home-assistant")')
    assert wait < card.index("customElements.define(")
    assert wait < card.index("./hubinet-ops-guest-cards.js")
    for card_type in (
        "hubinet-ops-lxc-card",
        "hubinet-ops-lxc-mini-card",
        "hubinet-ops-vm-card",
        "hubinet-ops-vm-mini-card",
    ):
        assert f'"{card_type}"' in guest_logic
    for name in ("LXC", "LXC mini", "VM", "VM mini"):
        assert f'name: "Hubinet-Ops {name}"' in guest_cards
    assert "static getConfigForm()" in guest_cards
    for source in (guest_cards, guest_logic):
        assert "http://" not in source
        assert 'from "' not in source
        assert "unpkg" not in source
        assert "cdn" not in source


def _role_table(logic: str, name: str) -> set[tuple[str, str]]:
    block = logic.split(f"export const {name} = {{", 1)[1].split("\n};", 1)[0]
    return set(re.findall(r'^  \w+: \["(\w+)", "(\w+)"\],$', block, re.MULTILINE))


def test_guest_card_roles_match_integration_translation_keys() -> None:
    """Every LXC and VM card role names a key those entities really define."""
    logic = (FRONTEND_DIRECTORY / "guest-card-logic.js").read_text(encoding="utf-8")
    snapshots = {
        ("button", SNAPSHOT_RESTORE_BUTTON.translation_key),
        ("button", SNAPSHOT_DELETE_BUTTON.translation_key),
        ("select", SNAPSHOT_SELECT.translation_key),
    }
    lxc = _role_table(logic, "LXC_ROLES")
    vm = _role_table(logic, "VM_ROLES")
    assert len(lxc) == 16
    assert len(vm) == 19
    assert lxc <= {
        *(("sensor", d.translation_key) for d in CONTAINER_SENSORS),
        *(("button", d.translation_key) for d in CONTAINER_BUTTONS),
        *snapshots,
    }
    assert vm <= {
        *(("sensor", d.translation_key) for d in VM_SENSORS),
        *(("button", d.translation_key) for d in VM_BUTTONS),
        *snapshots,
    }
    # Restart has no translation key; the cards match its device class.
    for buttons in (CONTAINER_BUTTONS, VM_BUTTONS):
        restart = next(d for d in buttons if d.key == "restart")
        assert restart.translation_key is None
        assert restart.device_class == "restart"
    # The editor filters devices by the models the integration registers.
    entity_source = (FRONTEND_DIRECTORY.parent / "entity.py").read_text(
        encoding="utf-8"
    )
    for model in ("Container", "VM"):
        assert f'model="{model}"' in entity_source
        assert f'model: "{model}"' in logic
