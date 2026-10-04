"""Tests for the Proxmox VE sensor platform."""

from copy import deepcopy
import math
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from syrupy.assertion import SnapshotAssertion
from freezegun.api import FrozenDateTimeFactory
from tests.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_load_json_array_fixture,
    snapshot_platform,
)

from custom_components.hubinet_ops.const import CONF_VM_GUEST_MEMORY
from custom_components.hubinet_ops.coordinator import DEFAULT_UPDATE_INTERVAL
from custom_components.hubinet_ops.sensor import VM_SENSORS
from custom_components.hubinet_ops.vm_memory import (
    vm_guest_memory,
    vm_guest_memory_percentage,
    vm_host_memory,
    vm_host_memory_percentage,
)

from . import PVEVMUSER_PERMISSIONS, setup_integration


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> None:
    """Make sure all entities are enabled."""


async def test_all_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test all entities."""
    with patch(
        "custom_components.hubinet_ops.PLATFORMS",
        [Platform.SENSOR],
    ):
        await setup_integration(hass, mock_config_entry)
        await snapshot_platform(
            hass,
            entity_registry,
            snapshot,
            mock_config_entry.entry_id,
        )


async def test_storage_missing_used_fraction(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test storage usage percentage sensor when used_fraction is missing."""
    storage_data = await async_load_json_array_fixture(
        hass, "nodes/storage.json", "hubinet_ops"
    )
    # Remove used_fraction from all storage entries
    storage_without_fraction = [
        {key: value for key, value in storage.items() if key != "used_fraction"}
        for storage in storage_data
    ]
    mock_proxmox_client._node_mock.storage.get.return_value = storage_without_fraction

    with patch(
        "custom_components.hubinet_ops.PLATFORMS",
        [Platform.SENSOR],
    ):
        await setup_integration(hass, mock_config_entry)

    state = hass.states.get("sensor.storage_local_storage_usage_percentage")
    assert state.state == STATE_UNKNOWN


async def test_sensors_according_to_permissions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Test that sensors are not created when not allowed."""
    mock_proxmox_client.access.permissions.get.return_value = PVEVMUSER_PERMISSIONS

    with patch(
        "custom_components.hubinet_ops.PLATFORMS",
        [Platform.SENSOR],
    ):
        await setup_integration(hass, mock_config_entry)

    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )

    assert "sensor.pve1_status" in {e.entity_id for e in entries}
    assert "sensor.pve1_cpu" not in {e.entity_id for e in entries}


# --- Checkpoint D: host and guest memory of a QEMU VM ------------------------

GIB = 1024**3
MAXMEM = 6 * GIB
TOTAL = 6219169792
FREE = 141623296
GUEST = TOTAL - FREE  # 6077546496 B, about 5.66 GiB, 94.3 %
HOST = 6378209280  # about 5.94 GiB, 99.0 %

HOST_BYTES = "sensor.vm_web_memory_usage"
HOST_PCT = "sensor.vm_web_memory_usage_percentage"
GUEST_BYTES = "sensor.vm_web_guest_memory_usage"
GUEST_PCT = "sensor.vm_web_guest_memory_usage_percentage"


def _full_vm(**changes: Any) -> dict[str, Any]:
    """Return a running VM as `qemu.get(full=1)` lists it, with the owner's fields.

    The field set is the owner's VM100 payload (mem, memhost, maxmem, freemem,
    balloon, ballooninfo.actual/free_mem/max_mem/total_mem); the numbers here
    are test values. A change of `...` removes that key.
    """
    vm: dict[str, Any] = {
        "vmid": 100,
        "name": "vm-web",
        "status": "running",
        "cpus": 2,
        "cpu": 0.15,
        "maxdisk": 34359738368,
        "disk": 1234567890,
        "uptime": 86400,
        "netin": 1048576,
        "netout": 524288,
        "maxmem": MAXMEM,
        "mem": GUEST,
        "memhost": HOST,
        "freemem": FREE,
        "balloon": MAXMEM,
        "ballooninfo": {
            "actual": MAXMEM,
            "max_mem": MAXMEM,
            "total_mem": TOTAL,
            "free_mem": FREE,
        },
    }
    for key, value in changes.items():
        if value is ...:
            vm.pop(key)
        else:
            vm[key] = value
    return vm


def _balloon(**changes: Any) -> dict[str, Any]:
    """Return a full VM whose balloon info has the given changes."""
    vm = _full_vm()
    for key, value in changes.items():
        if value is ...:
            vm["ballooninfo"].pop(key)
        else:
            vm["ballooninfo"][key] = value
    return vm


def test_full_listing_owner_fields_give_host_and_guest() -> None:
    """Host is memhost, guest is the consistent balloon view, both of maxmem."""
    vm = _full_vm()
    assert vm_host_memory(vm) == HOST
    assert vm_guest_memory(vm) == GUEST
    assert vm_host_memory_percentage(vm) == pytest.approx(99.0029, abs=1e-3)
    assert vm_guest_memory_percentage(vm) == pytest.approx(94.3359, abs=1e-3)
    assert vm_host_memory(vm) != vm_guest_memory(vm)


def test_zero_is_a_value_not_unknown() -> None:
    """An idle guest or host reports 0, never unknown."""
    vm = _full_vm(mem=0, memhost=0, freemem=TOTAL)
    vm["ballooninfo"]["free_mem"] = TOTAL
    assert vm_host_memory(vm) == 0
    assert vm_guest_memory(vm) == 0
    assert vm_host_memory_percentage(vm) == 0
    assert vm_guest_memory_percentage(vm) == 0
    empty = _full_vm(mem=0, freemem=0)
    empty["ballooninfo"].update(total_mem=0, free_mem=0)
    assert vm_guest_memory(empty) == 0


@pytest.mark.parametrize(
    "vm",
    [
        pytest.param(_full_vm(status="stopped"), id="not-running"),
        pytest.param(_full_vm(status=...), id="no-status"),
        pytest.param(_full_vm(ballooninfo=...), id="no-ballooninfo"),
        pytest.param(_full_vm(ballooninfo=None), id="null-ballooninfo"),
        pytest.param(_full_vm(ballooninfo="6442450944"), id="malformed-text"),
        pytest.param(_full_vm(ballooninfo=[TOTAL, FREE]), id="malformed-list"),
        pytest.param(_full_vm(ballooninfo={}), id="empty"),
        pytest.param(_balloon(max_mem=...), id="incomplete-max"),
        pytest.param(_balloon(total_mem=...), id="incomplete-total"),
        pytest.param(_balloon(free_mem=...), id="incomplete-free"),
        pytest.param(_balloon(total_mem=None), id="null-total"),
        pytest.param(_balloon(free_mem=None), id="null-free"),
        pytest.param(_balloon(total_mem=str(TOTAL)), id="text-total"),
        pytest.param(_balloon(free_mem=str(FREE)), id="text-free"),
        pytest.param(_balloon(total_mem=True), id="bool-total"),
        pytest.param(_balloon(free_mem=math.nan), id="nan-free"),
        pytest.param(_balloon(total_mem=math.inf), id="infinite-total"),
        pytest.param(_balloon(free_mem=-1), id="negative-free"),
        pytest.param(_balloon(free_mem=TOTAL + 1), id="free-above-total"),
        pytest.param(_balloon(max_mem=0), id="zero-max"),
        pytest.param(_balloon(max_mem=-MAXMEM), id="negative-max"),
        pytest.param(_balloon(max_mem="6442450944"), id="text-max"),
        pytest.param(_full_vm(mem=GUEST + 4096), id="mem-above-total-minus-free"),
        pytest.param(_full_vm(mem=HOST), id="mem-is-the-host-value"),
        pytest.param(_full_vm(mem=...), id="no-mem"),
        pytest.param(_full_vm(mem=str(GUEST)), id="text-mem"),
        pytest.param(_full_vm(freemem=FREE + 4096), id="contradicting-freemem"),
    ],
)
def test_untrustworthy_balloon_data_is_unknown_and_never_the_host_value(
    vm: dict[str, Any],
) -> None:
    """Guest memory is published only when the payload establishes it."""
    assert vm_guest_memory(vm) is None
    assert vm_guest_memory_percentage(vm) is None
    # Host keeps its own truth and never depends on guest validation.
    assert vm_host_memory(vm) == HOST
    assert vm_host_memory_percentage(vm) == pytest.approx(99.0029, abs=1e-3)


def test_host_above_maximum_is_not_clamped() -> None:
    """Raw host use above maxmem stays above 100 %."""
    vm = _full_vm(memhost=MAXMEM + GIB // 2)
    assert vm_host_memory(vm) == MAXMEM + GIB // 2
    assert vm_host_memory_percentage(vm) == pytest.approx(108.3333, abs=1e-3)
    assert vm_guest_memory_percentage(vm) == pytest.approx(94.3359, abs=1e-3)


def test_host_falls_back_to_mem_only_without_any_guest_evidence() -> None:
    """`mem` is host memory only when nothing suggests PVE replaced it."""
    plain = _full_vm(memhost=..., freemem=..., ballooninfo=..., balloon=..., mem=GIB)
    assert vm_host_memory(plain) == GIB
    assert vm_host_memory_percentage(plain) == pytest.approx(100 / 6)
    assert vm_guest_memory(plain) is None
    # Balloon info without guest statistics does not replace `mem` either.
    actual_only = _full_vm(memhost=..., freemem=..., mem=GIB)
    actual_only["ballooninfo"] = {"actual": MAXMEM, "max_mem": MAXMEM}
    assert vm_host_memory(actual_only) == GIB
    assert vm_guest_memory(actual_only) is None


@pytest.mark.parametrize(
    "vm",
    [
        pytest.param(_full_vm(memhost=...), id="valid-guest-data"),
        pytest.param(_full_vm(memhost=..., mem=GUEST + 4096), id="mismatched-mem"),
        pytest.param(_full_vm(memhost=..., ballooninfo=...), id="freemem-only"),
        pytest.param(
            {**_balloon(free_mem=...), "freemem": ..., "memhost": ...},
            id="incomplete-balloon",
        ),
        pytest.param(
            _full_vm(memhost=..., freemem=..., ballooninfo="broken"),
            id="malformed-balloon",
        ),
    ],
)
def test_failed_guest_validation_does_not_make_mem_the_host_value(
    vm: dict[str, Any],
) -> None:
    """Without memhost, `mem` that may be guest-side is never called host."""
    vm = {key: value for key, value in vm.items() if value is not ...}
    assert vm_host_memory(vm) is None
    assert vm_host_memory_percentage(vm) is None


@pytest.mark.parametrize("memhost", [None, "6378209280", True, -1, math.nan, math.inf])
def test_malformed_memhost_is_unknown_without_a_fallback(memhost: Any) -> None:
    """A present but unusable memhost is unknown, not replaced by `mem`."""
    vm = _full_vm(memhost=memhost)
    assert vm_host_memory(vm) is None
    assert vm_host_memory_percentage(vm) is None
    assert vm_guest_memory(vm) == GUEST


@pytest.mark.parametrize("maxmem", [0, -1, None, "6442450944", ...])
def test_percentages_need_a_positive_maximum(maxmem: Any) -> None:
    """Without a positive maxmem the amounts stay; the percentages are unknown."""
    vm = _full_vm(maxmem=maxmem)
    assert vm_host_memory(vm) == HOST
    assert vm_guest_memory(vm) == GUEST
    assert vm_host_memory_percentage(vm) is None
    assert vm_guest_memory_percentage(vm) is None


async def _setup_sensors(
    hass: HomeAssistant,
    client: MagicMock,
    entry: MockConfigEntry,
    vm: dict[str, Any],
    *,
    enabled: bool,
) -> None:
    client._node_mock.qemu.get.return_value = [deepcopy(vm)]  # noqa: SLF001
    if enabled:
        object.__setattr__(
            entry, "options", MappingProxyType({CONF_VM_GUEST_MEMORY: True})
        )
    with patch("custom_components.hubinet_ops.PLATFORMS", [Platform.SENSOR]):
        await setup_integration(hass, entry)


def _number(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


async def test_option_off_is_host_only_from_mem(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Off keeps the previous host sensors from `mem`; guest stays unknown."""
    # Even a payload that carries full fields is not interpreted when off.
    await _setup_sensors(
        hass, mock_proxmox_client, mock_config_entry, _full_vm(), enabled=False
    )
    assert _number(hass, HOST_BYTES) == pytest.approx(GUEST / GIB)
    assert _number(hass, HOST_PCT) == pytest.approx(GUEST / MAXMEM * 100)
    assert hass.states.get(GUEST_BYTES).state == STATE_UNKNOWN
    assert hass.states.get(GUEST_PCT).state == STATE_UNKNOWN


async def test_option_on_publishes_host_from_memhost_and_guest_from_balloon(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """On: the existing sensors are host memory, the new ones guest memory."""
    await _setup_sensors(
        hass, mock_proxmox_client, mock_config_entry, _full_vm(), enabled=True
    )
    assert _number(hass, HOST_BYTES) == pytest.approx(HOST / GIB)
    assert _number(hass, HOST_PCT) == pytest.approx(HOST / MAXMEM * 100)
    assert _number(hass, GUEST_BYTES) == pytest.approx(GUEST / GIB)
    assert _number(hass, GUEST_PCT) == pytest.approx(GUEST / MAXMEM * 100)
    assert _number(hass, "sensor.vm_web_max_memory_usage") == pytest.approx(6)


async def test_option_on_with_invalid_balloon_keeps_guest_unknown(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Guest unknown never copies host, and host never copies `mem`."""
    await _setup_sensors(
        hass,
        mock_proxmox_client,
        mock_config_entry,
        _balloon(free_mem=TOTAL + 1),
        enabled=True,
    )
    assert hass.states.get(GUEST_BYTES).state == STATE_UNKNOWN
    assert hass.states.get(GUEST_PCT).state == STATE_UNKNOWN
    assert _number(hass, HOST_BYTES) == pytest.approx(HOST / GIB)
    assert _number(hass, HOST_PCT) == pytest.approx(HOST / MAXMEM * 100)


async def test_option_on_without_memhost_and_with_guest_evidence_is_unknown(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Host is unknown rather than a possibly guest-side `mem`."""
    await _setup_sensors(
        hass,
        mock_proxmox_client,
        mock_config_entry,
        _full_vm(memhost=..., mem=GUEST + 4096),
        enabled=True,
    )
    for entity_id in (HOST_BYTES, HOST_PCT, GUEST_BYTES, GUEST_PCT):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_option_on_host_above_100_percent_and_zero_values(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Telemetry is not clamped, and zero is published as zero."""
    await _setup_sensors(
        hass,
        mock_proxmox_client,
        mock_config_entry,
        _full_vm(memhost=MAXMEM + GIB // 2),
        enabled=True,
    )
    assert _number(hass, HOST_PCT) == pytest.approx(108.3333, abs=1e-3)
    assert _number(hass, GUEST_PCT) == pytest.approx(94.3359, abs=1e-3)

    idle = _full_vm(mem=0, memhost=0, freemem=TOTAL)
    idle["ballooninfo"]["free_mem"] = TOTAL
    mock_proxmox_client._node_mock.qemu.get.return_value = [idle]  # noqa: SLF001
    freezer.tick(DEFAULT_UPDATE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    for entity_id in (HOST_BYTES, HOST_PCT, GUEST_BYTES, GUEST_PCT):
        assert _number(hass, entity_id) == 0


@pytest.mark.parametrize("enabled", [False, True])
async def test_memory_entities_are_stable_whatever_the_option(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    enabled: bool,
) -> None:
    """The option changes values only: same entities, unique IDs and metadata."""
    await _setup_sensors(
        hass, mock_proxmox_client, mock_config_entry, _full_vm(), enabled=enabled
    )
    expected = {
        HOST_BYTES: "1234_100_vm_memory",
        HOST_PCT: "1234_100_vm_memory_percentage",
        "sensor.vm_web_max_memory_usage": "1234_100_vm_max_memory",
        GUEST_BYTES: "1234_100_vm_guest_memory",
        GUEST_PCT: "1234_100_vm_guest_memory_percentage",
    }
    for entity_id, unique_id in expected.items():
        registered = entity_registry.async_get(entity_id)
        assert registered is not None
        assert registered.unique_id == unique_id
        assert registered.translation_key == unique_id.removeprefix("1234_100_")
        assert hass.states.get(entity_id) is not None
    # History identity of the host sensors: unit, class and statistics kind.
    host = hass.states.get(HOST_BYTES).attributes
    assert host["unit_of_measurement"] == "GiB"
    assert host["device_class"] == "data_size"
    assert host["state_class"] == "measurement"
    percentage = hass.states.get(HOST_PCT).attributes
    assert percentage["unit_of_measurement"] == "%"
    assert percentage["state_class"] == "measurement"


def test_guest_sensors_are_added_beside_unchanged_host_descriptions() -> None:
    """Host keys keep their legacy reader; guest keys read nothing when off."""
    descriptions = {description.key: description for description in VM_SENSORS}
    vm = _full_vm()
    assert descriptions["vm_memory"].value_fn(vm) == vm["mem"]
    assert descriptions["vm_memory_percentage"].value_fn(vm) == pytest.approx(
        vm["mem"] / MAXMEM * 100
    )
    assert descriptions["vm_memory"].full_value_fn is vm_host_memory
    assert (
        descriptions["vm_memory_percentage"].full_value_fn is vm_host_memory_percentage
    )
    for key, reader in (
        ("vm_guest_memory", vm_guest_memory),
        ("vm_guest_memory_percentage", vm_guest_memory_percentage),
    ):
        assert descriptions[key].value_fn(vm) is None
        assert descriptions[key].full_value_fn is reader
    # No other VM sensor changes with the option.
    assert {
        key for key, description in descriptions.items() if description.full_value_fn
    } == {
        "vm_memory",
        "vm_memory_percentage",
        "vm_guest_memory",
        "vm_guest_memory_percentage",
    }
