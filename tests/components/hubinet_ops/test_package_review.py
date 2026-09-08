"""Tests for the package-review Home Assistant action surface.

These exercise `hubinet_ops.get_package_plan` and
`hubinet_ops.confirm_package_review` as real entity-service calls (target
resolution, `required_features` filtering, and multi-target dispatch)
rather than calling `PackageManager` directly -- see test_packages.py for
manager-level review state and concurrency coverage.
"""

import asyncio
from copy import deepcopy
import json
from unittest.mock import MagicMock, patch

import pytest
from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.packages.models import (
    PackageScanError,
    PackageScanFailure,
)
from custom_components.hubinet_ops.sensor import (
    ATTR_TOKEN,
    SERVICE_CONFIRM_PACKAGE_REVIEW,
    SERVICE_GET_PACKAGE_PLAN,
)
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import setup_integration
from .test_packages import GateTransport

CT_NGINX_SENSOR = "sensor.ct_nginx_pending_package_updates"
CT_NGINX_BUTTON = "button.ct_nginx_scan_pending_packages"
CT_BACKUP_SENSOR = "sensor.ct_backup_pending_package_updates"
CT_BACKUP_BUTTON = "button.ct_backup_scan_pending_packages"


async def _press_scan(hass: HomeAssistant, button_entity_id: str) -> None:
    """Press a package-scan button without waiting for the SSH transport."""
    await hass.services.async_call(
        "button",
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: button_entity_id},
        blocking=True,
    )


async def _run_scan(hass: HomeAssistant, transport: GateTransport, vmid: int) -> None:
    """Trigger, then release and wait out, one gated scan."""
    await asyncio.wait_for(transport.entered(vmid).wait(), 1)
    transport.release(vmid)
    await asyncio.wait_for(transport.completed(vmid).wait(), 1)
    await hass.async_block_till_done()


def _patched_transport(transport: GateTransport):
    """Return the AsyncSSHPackageTransport.async_scan patch context manager."""

    async def fake_scan(_self, expected_node, vmid):
        return await transport.async_scan(expected_node, vmid)

    return patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=fake_scan,
    )


async def _setup_with_ct_nginx_scanned(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> GateTransport:
    """Set up the integration with one successful CT nginx (vmid 200) scan."""
    transport = GateTransport()
    with _patched_transport(transport):
        await setup_integration(hass, mock_config_entry)
        await _press_scan(hass, CT_NGINX_BUTTON)
        await _run_scan(hass, transport, 200)
    return transport


async def test_get_package_plan_returns_the_exact_plan_and_a_token(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H1: a successful scan's exact plan, status, token, and reviewed flag."""
    await _setup_with_ct_nginx_scanned(hass, mock_config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_PACKAGE_PLAN,
        {ATTR_ENTITY_ID: CT_NGINX_SENSOR},
        blocking=True,
        return_response=True,
    )

    plan = response[CT_NGINX_SENSOR]
    assert plan["status"] == "success"
    assert isinstance(plan["token"], str) and plan["token"]
    assert plan["reviewed"] is False
    assert plan["packages"] == [
        {
            "name": "openssl",
            "architecture": "amd64",
            "installed_version": "1.0",
            "candidate_version": "1.1",
            "origin": "Debian-Security",
            "security": True,
        },
        {
            "name": "example",
            "architecture": "amd64",
            "installed_version": "1.0",
            "candidate_version": "1.1",
            "origin": None,
            "security": None,
        },
    ]


async def test_get_package_plan_never_scan_returns_status_only(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H2: a never-scanned target returns only `status`, no token or rows."""
    await setup_integration(hass, mock_config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_PACKAGE_PLAN,
        {ATTR_ENTITY_ID: CT_NGINX_SENSOR},
        blocking=True,
        return_response=True,
    )

    assert response[CT_NGINX_SENSOR] == {"status": "never"}


async def test_get_package_plan_failed_scan_returns_status_only(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H2: a failed scan also returns only `status`, no token or rows."""
    transport = GateTransport()
    transport.outcomes[200] = PackageScanError(
        PackageScanFailure.SIMULATION_FAILED, "APT simulation failed"
    )
    with _patched_transport(transport):
        await setup_integration(hass, mock_config_entry)
        await _press_scan(hass, CT_NGINX_BUTTON)
        await _run_scan(hass, transport, 200)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_PACKAGE_PLAN,
        {ATTR_ENTITY_ID: CT_NGINX_SENSOR},
        blocking=True,
        return_response=True,
    )

    assert response[CT_NGINX_SENSOR] == {"status": "failed"}


async def test_confirm_package_review_with_current_token_succeeds(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H3: confirming with the current token marks the plan reviewed."""
    await _setup_with_ct_nginx_scanned(hass, mock_config_entry)

    plan = (
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_PACKAGE_PLAN,
            {ATTR_ENTITY_ID: CT_NGINX_SENSOR},
            blocking=True,
            return_response=True,
        )
    )[CT_NGINX_SENSOR]

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_CONFIRM_PACKAGE_REVIEW,
        {ATTR_ENTITY_ID: CT_NGINX_SENSOR, ATTR_TOKEN: plan["token"]},
        blocking=True,
        return_response=True,
    )
    assert response[CT_NGINX_SENSOR] == {"reviewed": True}

    state = hass.states.get(CT_NGINX_SENSOR)
    assert state is not None
    assert state.attributes["reviewed"] is True


async def test_confirm_package_review_with_stale_token_is_rejected(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H4: a stale/wrong token is an ordinary `reviewed: false` response."""
    await _setup_with_ct_nginx_scanned(hass, mock_config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_CONFIRM_PACKAGE_REVIEW,
        {ATTR_ENTITY_ID: CT_NGINX_SENSOR, ATTR_TOKEN: "not-the-current-token"},
        blocking=True,
        return_response=True,
    )
    assert response[CT_NGINX_SENSOR] == {"reviewed": False}

    state = hass.states.get(CT_NGINX_SENSOR)
    assert state is not None
    assert state.attributes["reviewed"] is False


async def test_multi_target_confirmation_is_independent_per_sensor(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H5: one target's mismatched token must not fail another's confirmation.

    A single confirmation call cannot carry a different token per target,
    so this presents CT nginx's own current token while also targeting CT
    backup: CT nginx must confirm and CT backup must be rejected, both as
    ordinary response data in one successful call -- not a raised
    cross-target exception once CT nginx has already succeeded.
    """
    containers = deepcopy(mock_proxmox_client._node_mock.lxc.get.return_value)  # noqa: SLF001
    for container in containers:
        container["status"] = "running"
    mock_proxmox_client._node_mock.lxc.get.return_value = containers  # noqa: SLF001

    transport = GateTransport()
    with _patched_transport(transport):
        await setup_integration(hass, mock_config_entry)
        await _press_scan(hass, CT_NGINX_BUTTON)
        await _run_scan(hass, transport, 200)
        await _press_scan(hass, CT_BACKUP_BUTTON)
        await _run_scan(hass, transport, 201)

    coordinator = mock_config_entry.runtime_data
    ct_nginx_token = coordinator.package_manager.record("pve1", 200).token

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_CONFIRM_PACKAGE_REVIEW,
        {
            ATTR_ENTITY_ID: [CT_NGINX_SENSOR, CT_BACKUP_SENSOR],
            ATTR_TOKEN: ct_nginx_token,
        },
        blocking=True,
        return_response=True,
    )

    assert response[CT_NGINX_SENSOR] == {"reviewed": True}
    assert response[CT_BACKUP_SENSOR] == {"reviewed": False}
    assert coordinator.package_manager.record("pve1", 200).reviewed is True
    assert coordinator.package_manager.record("pve1", 201).reviewed is False


async def test_required_features_excludes_ordinary_sensors(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H6: an ordinary Hubinet-Ops sensor does not support package review."""
    await setup_integration(hass, mock_config_entry)

    entity_registry = er.async_get(hass)
    package_entry = entity_registry.async_get(CT_NGINX_SENSOR)
    assert package_entry is not None
    other_sensor = next(
        entry.entity_id
        for entry in er.async_entries_for_device(
            entity_registry, package_entry.device_id
        )
        if entry.entity_id != CT_NGINX_SENSOR and entry.domain == "sensor"
    )

    with pytest.raises(ServiceNotSupported):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_PACKAGE_PLAN,
            {ATTR_ENTITY_ID: other_sensor},
            blocking=True,
            return_response=True,
        )


async def test_device_targeting_never_reaches_ordinary_sensors(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H7: a device target cannot invoke package review on ordinary sensors.

    A device's package sensor is a diagnostic entity, and Home Assistant's
    default device-target expansion (``primary_entities_only=True``)
    already excludes diagnostic/config entities from indirect device
    expansion -- this is native Core behavior this integration does not
    customize, not a package-review-specific routing layer. The practical
    effect matches the binding requirement either way: targeting the LXC's
    device as a whole cannot invoke package review on its ordinary (CPU,
    memory, status, ...) sensors, because indirect expansion excludes the
    package sensor entirely rather than narrowing to it, leaving nothing
    eligible for this response-required call -- Core's own caller-visible
    failure (see H8), not a silent call to an ordinary sensor. Reaching the
    package sensor itself requires naming its entity_id directly (H1-H5),
    exactly like any other diagnostic entity.
    """
    await setup_integration(hass, mock_config_entry)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{mock_config_entry.entry_id}_container_200"),
        config_entry_id=mock_config_entry.entry_id,
    )
    assert device is not None

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_PACKAGE_PLAN,
            {ATTR_DEVICE_ID: device.id},
            blocking=True,
            return_response=True,
        )


async def test_stopped_target_yields_native_caller_visible_failure(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H8: an unavailable (stopped) sole target is Core's native failure.

    No special Hubinet-Ops unavailable-target routing is added; a
    response-required call with no eligible target left relies on Home
    Assistant Core's own caller-visible failure.
    """
    await _setup_with_ct_nginx_scanned(hass, mock_config_entry)

    stopped_containers = deepcopy(
        mock_proxmox_client._node_mock.lxc.get.return_value  # noqa: SLF001
    )
    for container in stopped_containers:
        if container["vmid"] == "200":
            container["status"] = "stopped"
    mock_proxmox_client._node_mock.lxc.get.return_value = stopped_containers  # noqa: SLF001
    coordinator = mock_config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(CT_NGINX_SENSOR).state == "unavailable"

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_PACKAGE_PLAN,
            {ATTR_ENTITY_ID: CT_NGINX_SENSOR},
            blocking=True,
            return_response=True,
        )


async def test_review_actions_do_not_block_native_proxmox_button(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H9: package-review actions never contend with the button semaphore.

    Both actions are registered on the sensor platform, where
    `PARALLEL_UPDATES = 0` holds no entity-platform semaphore; the button
    platform's own `PARALLEL_UPDATES = 1` is unrelated to it.
    """
    await _setup_with_ct_nginx_scanned(hass, mock_config_entry)

    await asyncio.wait_for(
        asyncio.gather(
            hass.services.async_call(
                DOMAIN,
                SERVICE_GET_PACKAGE_PLAN,
                {ATTR_ENTITY_ID: CT_NGINX_SENSOR},
                blocking=True,
                return_response=True,
            ),
            hass.services.async_call(
                "button",
                SERVICE_PRESS,
                {ATTR_ENTITY_ID: "button.ct_nginx_restart"},
                blocking=True,
            ),
        ),
        1,
    )

    mock_proxmox_client.nodes("pve1").lxc(
        200
    ).status.reboot.post.assert_called_once_with()


async def test_token_and_packages_are_absent_from_entity_state(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """H10: entity attributes carry `reviewed`, never the token or rows."""
    await _setup_with_ct_nginx_scanned(hass, mock_config_entry)

    state = hass.states.get(CT_NGINX_SENSOR)
    assert state is not None
    assert "reviewed" in state.attributes
    assert "token" not in state.attributes
    assert "packages" not in state.attributes


async def test_get_package_plan_response_is_plain_json_serializable(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Requirement 17: package rows are primitive dicts, not dataclasses."""
    await _setup_with_ct_nginx_scanned(hass, mock_config_entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_PACKAGE_PLAN,
        {ATTR_ENTITY_ID: CT_NGINX_SENSOR},
        blocking=True,
        return_response=True,
    )

    plan = response[CT_NGINX_SENSOR]
    serialized = json.dumps(plan)
    reparsed = json.loads(serialized)
    assert reparsed == plan
    for row in plan["packages"]:
        assert type(row) is dict
        for value in row.values():
            assert value is None or isinstance(value, (str, bool))
