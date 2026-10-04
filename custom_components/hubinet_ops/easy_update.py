"""Thin Easy Update action over the existing package-manager entry points.

The owner-accepted Variant C action resolves one Hubinet-Ops Container device to
its existing ``(node, vmid)`` identity, then confirms the current successful scan
token and starts the existing Update in one synchronous event-loop callback. It
adds no package state: ``PackageManager`` stays authoritative for every check.
"""

import asyncio
from dataclasses import dataclass
import logging

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
from .snapshot_restore import snapshot_create_is_running

_LOGGER = logging.getLogger(__name__)
# How long YOLO waits for the Update and Health to finish.
AUTOREMOVE_OBSERVATION_SECONDS = 3600


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
                return EasyUpdateTarget(
                    entry, coordinator, node_data.node["node"], vmid
                )
    raise _invalid("easy_update_not_package_lxc")


@callback
def async_start_easy_update(
    hass: HomeAssistant,
    device_id: str,
    *,
    expected_scan_attempt: str | None = None,
    skip_snapshot: bool = False,
) -> tuple[EasyUpdateTarget, PackageUpdateRecord]:
    """Confirm the exact current plan and start the existing Update.

    Everything here is synchronous: no other work can interleave between
    reading the current scan, confirming its own token, and starting Update.
    """
    target = async_resolve_target(hass, device_id)
    coordinator, node, vmid = target.coordinator, target.node, target.vmid
    manager = coordinator.package_manager

    # Like native entities and Scan All, never act on stale Proxmox data.
    if not coordinator.last_update_success:
        raise _invalid("easy_update_not_running")
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
    if not skip_snapshot and not snapshot_permission:
        raise _rejected("VM.Snapshot permission is required for package updates")
    # Skip takes no snapshot of its own, so nothing else would make it wait
    # for this guest's running native Create; refuse before confirming.
    if skip_snapshot and snapshot_create_is_running(hass, coordinator, vmid):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="snapshot_create_running"
        )
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
            skip_snapshot=skip_snapshot,
        )
    except PackageUpdateError as err:
        raise _rejected(str(err)) from err
    return target, manager.update_record(node, vmid)


@callback
def async_start_post_update_autoremove(
    hass: HomeAssistant, target: EasyUpdateTarget, update: PackageUpdateRecord
) -> None:
    """Optionally follow one accepted Update with the existing Autoremove.

    One bounded, ephemeral, entry-tracked wait bound to the exact Update
    attempt. Health
    only has to stop running; its result is not a gate. Every other check
    stays in ``async_start_autoremove``.
    """
    coordinator, node, vmid = target.coordinator, target.node, target.vmid
    manager = coordinator.package_manager
    attempt = update.last_attempt
    if attempt is None:
        return

    def decide() -> bool | None:
        """Return None to keep waiting, False to stop, True to start."""
        if not coordinator.last_update_success:
            return False
        current = manager.update_record(node, vmid)
        if current.last_attempt != attempt:
            return False
        if current.status is PackageUpdateStatus.RUNNING:
            return None
        if current.status is not PackageUpdateStatus.SUCCESS:
            return False
        evidence = manager.cleanup_evidence(node, vmid)
        if (
            evidence is None
            or not evidence.candidates
            or evidence.observed_at < attempt
            or manager.restore_reserved(node, vmid)
        ):
            return False
        if (
            manager.health_record(node, vmid).check_status is HealthCheckStatus.RUNNING
            or manager.record(node, vmid).status is PackageScanStatus.RUNNING
            or manager.cleanup_record(node, vmid).status is PackageUpdateStatus.RUNNING
        ):
            return None
        node_data = (coordinator.data or {}).get(node)
        container = node_data.containers.get(vmid) if node_data is not None else None
        return (
            container is not None
            and container.get("status") == VM_CONTAINER_RUNNING
            and is_granted(
                coordinator.permissions,
                p_type="vms",
                p_id=vmid,
                permission=ProxmoxPermission.SNAPSHOT,
            )
        )

    async def follow() -> None:
        wake = asyncio.Event()
        remove_listener = coordinator.async_add_listener(wake.set)
        try:
            async with asyncio.timeout(AUTOREMOVE_OBSERVATION_SECONDS):
                while True:
                    wake.clear()
                    decision = decide()
                    if decision is not None:
                        break
                    await wake.wait()
        except TimeoutError:
            _LOGGER.info(
                "Easy Update did not start Autoremove for %s/%s: observation "
                "time ended",
                node,
                vmid,
            )
            return
        finally:
            remove_listener()
        if not decision:
            _LOGGER.debug("Easy Update skipped Autoremove for %s/%s", node, vmid)
            return
        try:
            manager.async_start_autoremove(
                node, vmid, target_is_running=True, snapshot_permission=True
            )
        except PackageUpdateError as err:
            _LOGGER.info(
                "Easy Update Autoremove for %s/%s was not started: %s",
                node,
                vmid,
                err,
            )

    target.entry.async_create_background_task(
        hass, follow(), f"easy update autoremove {node}/{vmid}"
    )
