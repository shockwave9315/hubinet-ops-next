"""Thin Easy Update action over the existing package-manager entry points.

The owner-accepted Variant C action resolves one Hubinet-Ops Container device to
its existing ``(node, vmid)`` identity, then confirms the current successful scan
token and starts the existing Update in one synchronous event-loop callback. It
adds no package state: ``PackageManager`` stays authoritative for every check.
"""

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, VM_CONTAINER_RUNNING, ProxmoxPermission
from .coordinator import ProxmoxConfigEntry, ProxmoxCoordinator
from .helpers import is_granted
from .packages.models import (
    HealthCheckStatus,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateRecord,
    PackageUpdateStatus,
)


@dataclass(frozen=True, slots=True)
class EasyUpdateTarget:
    """One resolved package-capable LXC in a loaded Hubinet-Ops entry."""

    entry: ProxmoxConfigEntry
    coordinator: ProxmoxCoordinator
    node: str
    vmid: int


def _invalid(translation_key: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=translation_key
    )


def _rejected(reason: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="package_update_failed",
        translation_placeholders={"reason": reason},
    )


@callback
def async_resolve_target(hass: HomeAssistant, device_id: str) -> EasyUpdateTarget:
    """Resolve a Container device by exact existing device identifiers only."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise _invalid("easy_update_unknown_device")
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            continue
        coordinator: ProxmoxCoordinator = entry.runtime_data
        if not coordinator.package_manager.configured or not coordinator.data:
            continue
        node_data = coordinator.data.get(coordinator.package_node)
        if node_data is None:
            continue
        for vmid in node_data.containers:
            # The same identifier the upstream-derived container entities use.
            if (DOMAIN, f"{entry.entry_id}_container_{vmid}") in device.identifiers:
                return EasyUpdateTarget(entry, coordinator, node_data.node["node"], vmid)
    raise _invalid("easy_update_not_package_lxc")


@callback
def async_start_easy_update(
    hass: HomeAssistant,
    device_id: str,
    *,
    expected_scan_attempt: str | None = None,
) -> tuple[EasyUpdateTarget, PackageUpdateRecord]:
    """Confirm the exact current plan and start the existing Update.

    Everything here is synchronous: no other work can interleave between
    reading the current scan, confirming its own token, and starting Update.
    """
    target = async_resolve_target(hass, device_id)
    coordinator, node, vmid = target.coordinator, target.node, target.vmid
    manager = coordinator.package_manager

    container = coordinator.data[node].containers.get(vmid)
    if container is None or container.get("status") != VM_CONTAINER_RUNNING:
        raise _invalid("easy_update_not_running")

    record = manager.record(node, vmid)
    if (
        record.status is not PackageScanStatus.SUCCESS
        or record.result is None
        or record.token is None
    ):
        raise _invalid("easy_update_no_current_scan")
    if not record.result.packages:
        raise _invalid("easy_update_nothing_to_update")
    if expected_scan_attempt is not None and (
        record.last_attempt is None
        or record.last_attempt.isoformat() != expected_scan_attempt
    ):
        raise _invalid("easy_update_scan_changed")

    # Read-only prechecks so predictable rejections leave no review state
    # behind; async_start_update below re-validates all of them itself.
    snapshot_permission = is_granted(
        coordinator.permissions,
        p_type="vms",
        p_id=vmid,
        permission=ProxmoxPermission.SNAPSHOT,
    )
    if not snapshot_permission:
        raise _rejected("VM.Snapshot permission is required for package updates")
    if (
        manager.restore_reserved(node, vmid)
        or manager.update_record(node, vmid).status is PackageUpdateStatus.RUNNING
        or manager.cleanup_record(node, vmid).status is PackageUpdateStatus.RUNNING
        or manager.health_record(node, vmid).check_status is HealthCheckStatus.RUNNING
    ):
        raise _rejected("another package operation is active for this LXC")

    if not manager.confirm_review(node, vmid, record.token):
        raise _invalid("easy_update_no_current_scan")
    try:
        manager.async_start_update(
            node,
            vmid,
            target_is_running=True,
            snapshot_permission=snapshot_permission,
        )
    except PackageUpdateError as err:
        raise _rejected(str(err)) from err
    return target, manager.update_record(node, vmid)
