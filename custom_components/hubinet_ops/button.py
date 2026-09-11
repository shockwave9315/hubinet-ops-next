"""Button platform for Proxmox VE."""

from abc import abstractmethod
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from html import escape
import logging
import re
from typing import Any, override

from proxmoxer import AuthenticationError
from proxmoxer.core import ResourceException
import requests
from requests.exceptions import ConnectTimeout, SSLError

from homeassistant.components import persistent_notification
from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.translation import async_get_cached_translations
from homeassistant.util import dt as dt_util

from .const import DOMAIN, VM_CONTAINER_RUNNING, ProxmoxPermission
from .coordinator import ProxmoxConfigEntry, ProxmoxCoordinator, ProxmoxNodeData
from .entity import ProxmoxContainerEntity, ProxmoxNodeEntity, ProxmoxVMEntity
from .helpers import is_granted
from .packages.models import (
    PackageScanError,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateStatus,
)
from .packages.presentation import notify_review_plan
from .select import (
    ATTR_SELECTED_SNAPSHOT,
    snapshot_select_unique_id,
    snapshot_selection_signal,
)
from .snapshots import (
    RestoreOutcome,
    RestoreResult,
    SnapshotKind,
    SnapshotListError,
    async_rollback_snapshot,
    async_validate_snapshot,
    snapshot_name_is_eligible,
)

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ProxmoxNodeButtonNodeEntityDescription(ButtonEntityDescription):
    """Class to hold Proxmox node button description."""

    press_action: Callable[[ProxmoxCoordinator, str], None]
    permission: ProxmoxPermission = ProxmoxPermission.SYSPOWER
    permission_target: str = "nodes"


@dataclass(frozen=True, kw_only=True)
class ProxmoxVMButtonEntityDescription(ButtonEntityDescription):
    """Class to hold Proxmox VM button description."""

    press_action: Callable[[ProxmoxCoordinator, str, int], None]
    permission: ProxmoxPermission = ProxmoxPermission.POWER
    permission_target: str = "vms"


@dataclass(frozen=True, kw_only=True)
class ProxmoxContainerButtonEntityDescription(ButtonEntityDescription):
    """Class to hold Proxmox container button description."""

    press_action: Callable[[ProxmoxCoordinator, str, int], None]
    permission: ProxmoxPermission = ProxmoxPermission.POWER
    permission_target: str = "vms"


NODE_BUTTONS: tuple[ProxmoxNodeButtonNodeEntityDescription, ...] = (
    ProxmoxNodeButtonNodeEntityDescription(
        key="reboot",
        press_action=lambda coordinator, node: coordinator.proxmox.nodes(
            node
        ).status.post(command="reboot"),
        entity_category=EntityCategory.CONFIG,
        device_class=ButtonDeviceClass.RESTART,
    ),
    ProxmoxNodeButtonNodeEntityDescription(
        key="shutdown",
        translation_key="shutdown",
        press_action=lambda coordinator, node: coordinator.proxmox.nodes(
            node
        ).status.post(command="shutdown"),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxNodeButtonNodeEntityDescription(
        key="start_all",
        translation_key="start_all",
        permission=ProxmoxPermission.POWER,
        permission_target="vms",
        press_action=lambda coordinator, node: coordinator.proxmox.nodes(
            node
        ).startall.post(),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxNodeButtonNodeEntityDescription(
        key="stop_all",
        translation_key="stop_all",
        permission=ProxmoxPermission.POWER,
        permission_target="vms",
        press_action=lambda coordinator, node: coordinator.proxmox.nodes(
            node
        ).stopall.post(),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxNodeButtonNodeEntityDescription(
        key="suspend_all",
        translation_key="suspend_all",
        permission=ProxmoxPermission.POWER,
        permission_target="vms",
        press_action=lambda coordinator, node: coordinator.proxmox.nodes(
            node
        ).suspendall.post(),
        entity_category=EntityCategory.CONFIG,
    ),
)

VM_BUTTONS: tuple[ProxmoxVMButtonEntityDescription, ...] = (
    ProxmoxVMButtonEntityDescription(
        key="start",
        translation_key="start",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).qemu(vmid).status.start.post()
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxVMButtonEntityDescription(
        key="stop",
        translation_key="stop",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).qemu(vmid).status.stop.post()
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxVMButtonEntityDescription(
        key="restart",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).qemu(vmid).status.reboot.post()
        ),
        entity_category=EntityCategory.CONFIG,
        device_class=ButtonDeviceClass.RESTART,
    ),
    ProxmoxVMButtonEntityDescription(
        key="pause",
        translation_key="pause",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).qemu(vmid).status.suspend.post()
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxVMButtonEntityDescription(
        key="hibernate",
        translation_key="hibernate",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).qemu(vmid).status.suspend.post(todisk=1)
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxVMButtonEntityDescription(
        key="resume",
        translation_key="resume",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).qemu(vmid).status.resume.post()
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxVMButtonEntityDescription(
        key="reset",
        translation_key="reset",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).qemu(vmid).status.reset.post()
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxVMButtonEntityDescription(
        key="shutdown",
        translation_key="shutdown",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).qemu(vmid).status.shutdown.post()
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxVMButtonEntityDescription(
        key="snapshot_create",
        translation_key="snapshot_create",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node)
            .qemu(vmid)
            .snapshot.post(
                snapname=f"homeassistant_snapshot_{dt_util.utcnow().strftime('%Y%m%d%H%M%S')}"
            )
        ),
        permission=ProxmoxPermission.SNAPSHOT,
        entity_category=EntityCategory.CONFIG,
    ),
)

CONTAINER_BUTTONS: tuple[ProxmoxContainerButtonEntityDescription, ...] = (
    ProxmoxContainerButtonEntityDescription(
        key="start",
        translation_key="start",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).lxc(vmid).status.start.post()
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxContainerButtonEntityDescription(
        key="stop",
        translation_key="stop",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).lxc(vmid).status.stop.post()
        ),
        entity_category=EntityCategory.CONFIG,
    ),
    ProxmoxContainerButtonEntityDescription(
        key="restart",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node).lxc(vmid).status.reboot.post()
        ),
        entity_category=EntityCategory.CONFIG,
        device_class=ButtonDeviceClass.RESTART,
    ),
    ProxmoxContainerButtonEntityDescription(
        key="snapshot_create",
        translation_key="snapshot_create",
        press_action=lambda coordinator, node, vmid: (
            coordinator.proxmox.nodes(node)
            .lxc(vmid)
            .snapshot.post(
                snapname=f"homeassistant_snapshot_{dt_util.utcnow().strftime('%Y%m%d%H%M%S')}"
            )
        ),
        permission=ProxmoxPermission.SNAPSHOT,
        entity_category=EntityCategory.CONFIG,
    ),
)

PACKAGE_SCAN_BUTTON = ButtonEntityDescription(
    key="package_scan",
    translation_key="package_scan",
    entity_category=EntityCategory.CONFIG,
)
PACKAGE_REVIEW_BUTTON = ButtonEntityDescription(
    key="package_review",
    translation_key="package_review",
    entity_category=EntityCategory.CONFIG,
)
PACKAGE_APPROVE_BUTTON = ButtonEntityDescription(
    key="package_approve",
    translation_key="package_approve",
    entity_category=EntityCategory.CONFIG,
)
PACKAGE_UPDATE_BUTTON = ButtonEntityDescription(
    key="package_update",
    translation_key="package_update",
    entity_category=EntityCategory.CONFIG,
)
PACKAGE_AUTOREMOVE_BUTTON = ButtonEntityDescription(
    key="package_autoremove",
    translation_key="package_autoremove",
    entity_category=EntityCategory.CONFIG,
)
SNAPSHOT_RESTORE_BUTTON = ButtonEntityDescription(
    key="snapshot_restore",
    translation_key="snapshot_restore",
    entity_category=EntityCategory.CONFIG,
)


def _has_restore_permissions(coordinator: ProxmoxCoordinator, vmid: int) -> bool:
    """Require the complete current native Restore permission set."""
    return all(
        is_granted(
            coordinator.permissions,
            p_type="vms",
            p_id=vmid,
            permission=permission,
        )
        for permission in (
            ProxmoxPermission.VMAUDIT,
            ProxmoxPermission.SNAPSHOT,
            ProxmoxPermission.POWER,
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ProxmoxVE buttons."""
    coordinator = entry.runtime_data

    def _async_add_new_nodes(nodes: list[ProxmoxNodeData]) -> None:
        """Add new node buttons."""
        async_add_entities(
            ProxmoxNodeButtonEntity(coordinator, entity_description, node)
            for node in nodes
            for entity_description in NODE_BUTTONS
            if is_granted(
                coordinator.permissions,
                p_type=entity_description.permission_target,
                p_id=node.node["node"],
                permission=entity_description.permission,
            )
        )

    def _async_add_new_vms(
        vms: list[tuple[ProxmoxNodeData, dict[str, Any]]],
    ) -> None:
        """Add new VM buttons."""
        entities: list[ButtonEntity] = [
            ProxmoxVMButtonEntity(coordinator, entity_description, vm, node_data)
            for (node_data, vm) in vms
            for entity_description in VM_BUTTONS
            if is_granted(
                coordinator.permissions,
                p_type=entity_description.permission_target,
                p_id=vm["vmid"],
                permission=entity_description.permission,
            )
        ]
        entities.extend(
            ProxmoxVMSnapshotRestoreButton(coordinator, vm, node_data)
            for node_data, vm in vms
            if _has_restore_permissions(coordinator, int(vm["vmid"]))
        )
        async_add_entities(entities)

    def _async_add_new_containers(
        containers: list[tuple[ProxmoxNodeData, dict[str, Any]]],
    ) -> None:
        """Add new container buttons."""
        entities: list[ButtonEntity] = [
            ProxmoxContainerButtonEntity(
                coordinator, entity_description, container, node_data
            )
            for (node_data, container) in containers
            for entity_description in CONTAINER_BUTTONS
            if is_granted(
                coordinator.permissions,
                p_type=entity_description.permission_target,
                p_id=container["vmid"],
                permission=entity_description.permission,
            )
        ]
        entities.extend(
            ProxmoxContainerSnapshotRestoreButton(coordinator, container, node_data)
            for node_data, container in containers
            if _has_restore_permissions(coordinator, int(container["vmid"]))
        )
        if coordinator.package_manager.configured:
            for node_data, container in containers:
                if node_data.node["node"] != coordinator.package_node:
                    continue
                entities.extend(
                    (
                        PackageScanButtonEntity(coordinator, container, node_data),
                        PackageReviewButtonEntity(coordinator, container, node_data),
                        PackageApproveButtonEntity(coordinator, container, node_data),
                    )
                )
                if is_granted(
                    coordinator.permissions,
                    p_type="vms",
                    p_id=container["vmid"],
                    permission=ProxmoxPermission.SNAPSHOT,
                ):
                    entities.append(
                        PackageUpdateButtonEntity(coordinator, container, node_data)
                    )
                    entities.append(
                        PackageAutoremoveButtonEntity(
                            coordinator, container, node_data
                        )
                    )
        async_add_entities(entities)

    coordinator.new_nodes_callbacks.append(_async_add_new_nodes)
    coordinator.new_vms_callbacks.append(_async_add_new_vms)
    coordinator.new_containers_callbacks.append(_async_add_new_containers)

    _async_add_new_nodes(
        [
            node_data
            for node_data in coordinator.data.values()
            if node_data.node["node"] in coordinator.known_nodes
        ]
    )
    _async_add_new_vms(
        [
            (node_data, vm_data)
            for node_data in coordinator.data.values()
            for vmid, vm_data in node_data.vms.items()
            if (node_data.node["node"], vmid) in coordinator.known_vms
        ]
    )
    _async_add_new_containers(
        [
            (node_data, container_data)
            for node_data in coordinator.data.values()
            for vmid, container_data in node_data.containers.items()
            if (node_data.node["node"], vmid) in coordinator.known_containers
        ]
    )


class ProxmoxBaseButton(ButtonEntity):
    """Common base for Proxmox buttons.

    Ensures the async_press logic isn't duplicated.
    """

    entity_description: ButtonEntityDescription
    coordinator: ProxmoxCoordinator

    @abstractmethod
    async def _async_press_call(self) -> None:
        """Abstract method used per Proxmox button class."""

    @override
    async def async_press(self) -> None:
        """Trigger the Proxmox button press service."""
        try:
            await self._async_press_call()
        except AuthenticationError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
            ) from err
        except SSLError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
            ) from err
        except ConnectTimeout as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="timeout_connect",
            ) from err
        except (ResourceException, requests.exceptions.ConnectionError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_error_details",
            ) from err


class ProxmoxNodeButtonEntity(ProxmoxNodeEntity, ProxmoxBaseButton):
    """Represents a Proxmox Node button entity."""

    entity_description: ProxmoxNodeButtonNodeEntityDescription

    @override
    async def _async_press_call(self) -> None:
        """Execute the node button action via executor."""
        await self.hass.async_add_executor_job(
            self.entity_description.press_action,
            self.coordinator,
            self._node_data.node["node"],
        )


class ProxmoxVMButtonEntity(ProxmoxVMEntity, ProxmoxBaseButton):
    """Represents a Proxmox VM button entity."""

    entity_description: ProxmoxVMButtonEntityDescription

    @override
    async def _async_press_call(self) -> None:
        """Execute the VM button action via executor."""
        await self.hass.async_add_executor_job(
            self.entity_description.press_action,
            self.coordinator,
            self._node_name,
            self.device_id,
        )


class ProxmoxContainerButtonEntity(ProxmoxContainerEntity, ProxmoxBaseButton):
    """Represents a Proxmox Container button entity."""

    entity_description: ProxmoxContainerButtonEntityDescription

    @override
    async def _async_press_call(self) -> None:
        """Execute the container button action via executor."""
        await self.hass.async_add_executor_job(
            self.entity_description.press_action,
            self.coordinator,
            self._node_name,
            self.device_id,
        )


def _translate_restore(
    hass: HomeAssistant, key: str, **placeholders: str
) -> str:
    """Return one cached localized Restore string with English fallback."""
    translation_key = f"component.{DOMAIN}.exceptions.{key}.message"
    for language in dict.fromkeys((hass.config.language, "en")):
        message = async_get_cached_translations(
            hass, language, "exceptions", DOMAIN
        ).get(translation_key)
        if message is None:
            continue
        try:
            return message.format(**placeholders)
        except (IndexError, KeyError, ValueError):
            continue
    return key


def _safe_restore_component(value: str) -> str:
    """Normalize one visible notification-ID component."""
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return normalized[:64] or "target"


def snapshot_restore_notification_id(
    entry_id: str, kind: SnapshotKind, node: str, vmid: int
) -> str:
    """Return a collision-resistant per-entry, per-guest Restore result ID."""
    identity = f"{entry_id}\0{kind}\0{node}\0{vmid}"
    digest = sha256(identity.encode()).hexdigest()[:12]
    return (
        f"snapshot_restore_{_safe_restore_component(entry_id)}_{kind}_"
        f"{_safe_restore_component(node)}_{vmid}_{digest}"
    )


def _notify_snapshot_restore(
    hass: HomeAssistant,
    entry_id: str,
    kind: SnapshotKind,
    node: str,
    vmid: int,
    snapshot_name: str,
    result: RestoreResult,
    *,
    package_truth_invalidated: bool,
) -> None:
    """Publish one replaceable, localized, bounded native Restore result."""
    target_type = _translate_restore(
        hass,
        "snapshot_restore_target_qemu"
        if kind is SnapshotKind.QEMU
        else "snapshot_restore_target_lxc",
    )
    placeholders = {
        "target_type": target_type,
        "node": escape(node),
        "vmid": str(vmid),
        "snapshot": escape(snapshot_name),
        "timestamp": dt_util.utcnow().isoformat(),
    }
    if result.outcome is RestoreOutcome.SUCCESS:
        key = "snapshot_restore_success_notification"
    elif result.outcome is RestoreOutcome.FAILED:
        key = "snapshot_restore_failed_notification"
    elif result.outcome is RestoreOutcome.NOT_STARTED:
        key = "snapshot_restore_not_started_notification"
    else:
        key = "snapshot_restore_uncertain_notification"
    message = _translate_restore(hass, key, **placeholders)
    if result.upid is not None:
        message += "\n\n" + _translate_restore(
            hass, "snapshot_restore_upid_detail", upid=escape(result.upid)
        )
    if result.reason:
        message += "\n\n" + _translate_restore(
            hass,
            "snapshot_restore_reason_detail",
            reason=escape(result.reason),
        )
    if package_truth_invalidated:
        message += "\n\n" + _translate_restore(
            hass, "snapshot_restore_lxc_package_detail"
        )
    if kind is SnapshotKind.LXC and result.outcome is RestoreOutcome.UNCERTAIN:
        message += "\n\n" + _translate_restore(
            hass, "snapshot_restore_uncertain_lxc_guidance"
        )
    persistent_notification.async_create(
        hass,
        message,
        _translate_restore(hass, "snapshot_restore_notification_title"),
        snapshot_restore_notification_id(entry_id, kind, node, vmid),
    )


class SnapshotRestoreButtonMixin(ButtonEntity):
    """Accept and launch one explicit native snapshot Restore."""

    coordinator: ProxmoxCoordinator
    device_id: int
    _kind: SnapshotKind
    _node_name: str

    def _selected_snapshot(self) -> str | None:
        """Read only the selector's explicit collision-safe identity attribute."""
        unique_id = snapshot_select_unique_id(
            self.coordinator.config_entry.entry_id,
            self._kind,
            self._node_name,
            self.device_id,
        )
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "select", DOMAIN, unique_id
        )
        state = self.hass.states.get(entity_id) if entity_id is not None else None
        selected = (
            state.attributes.get(ATTR_SELECTED_SNAPSHOT) if state is not None else None
        )
        return selected if snapshot_name_is_eligible(selected) else None

    def _target_exists(self) -> bool:
        """Require the target in current upstream-derived coordinator data."""
        node_data = self.coordinator.data.get(self._node_name)
        if node_data is None:
            return False
        targets = (
            node_data.vms
            if self._kind is SnapshotKind.QEMU
            else node_data.containers
        )
        return self.device_id in targets

    @override
    async def async_press(self) -> None:
        """Synchronously accept Restore, then release the button semaphore."""
        snapshot_name = self._selected_snapshot()
        if snapshot_name is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="snapshot_restore_no_selection",
            )
        if not self._target_exists():
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="snapshot_restore_guest_missing",
            )
        if not _has_restore_permissions(self.coordinator, self.device_id):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="snapshot_restore_permission_denied",
            )

        reserved = False
        if self._kind is SnapshotKind.LXC:
            try:
                self.coordinator.package_manager.begin_restore(
                    self._node_name, self.device_id
                )
            except PackageUpdateError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="snapshot_restore_busy",
                ) from err
            reserved = True

        try:
            async_dispatcher_send(
                self.hass,
                snapshot_selection_signal(
                    self.coordinator.config_entry.entry_id,
                    self._kind,
                    self._node_name,
                    self.device_id,
                ),
            )
        except Exception:
            _LOGGER.exception(
                "Could not clear accepted snapshot choice for %s/%s",
                self._node_name,
                self.device_id,
            )

        operation = self._async_run_restore(snapshot_name, reserved=reserved)
        try:
            self.coordinator.config_entry.async_create_background_task(
                self.hass,
                operation,
                f"snapshot Restore {self._node_name}/{self.device_id}",
            )
        except Exception:
            operation.close()
            if reserved:
                self.coordinator.package_manager.end_restore(
                    self._node_name, self.device_id
                )
            raise

    async def _async_run_restore(
        self, snapshot_name: str, *, reserved: bool
    ) -> None:
        """Run fresh validation, native submission, observation, and refresh."""
        result = RestoreResult(RestoreOutcome.NOT_STARTED)
        submission_may_have_started = False
        package_truth_invalidated = False
        cancelled = False
        try:
            try:
                valid = await async_validate_snapshot(
                    self.coordinator.proxmox,
                    self._node_name,
                    self.device_id,
                    self._kind,
                    snapshot_name,
                    executor=self.hass.async_add_executor_job,
                )
            except SnapshotListError as err:
                result = RestoreResult(RestoreOutcome.NOT_STARTED, reason=str(err))
            else:
                if not valid:
                    result = RestoreResult(
                        RestoreOutcome.NOT_STARTED,
                        reason="the selected snapshot is no longer eligible or present",
                    )
                else:
                    if self._kind is SnapshotKind.LXC:
                        self.coordinator.package_manager.invalidate_restore_target(
                            self._node_name, self.device_id
                        )
                        package_truth_invalidated = True
                    submission_may_have_started = True
                    result = await async_rollback_snapshot(
                        self.coordinator.proxmox,
                        self._node_name,
                        self.device_id,
                        self._kind,
                        snapshot_name,
                        executor=self.hass.async_add_executor_job,
                    )
        except asyncio.CancelledError:
            cancelled = True
            result = RestoreResult(
                RestoreOutcome.UNCERTAIN
                if submission_may_have_started
                else RestoreOutcome.NOT_STARTED,
                reason="Restore observation was interrupted",
            )
        except Exception:
            _LOGGER.exception(
                "Unexpected native snapshot Restore failure for %s/%s",
                self._node_name,
                self.device_id,
            )
            result = RestoreResult(
                RestoreOutcome.UNCERTAIN
                if submission_may_have_started
                else RestoreOutcome.NOT_STARTED,
                reason="unexpected Restore failure",
            )

        if reserved and result.outcome is not RestoreOutcome.UNCERTAIN:
            self.coordinator.package_manager.end_restore(
                self._node_name, self.device_id
            )
        try:
            _notify_snapshot_restore(
                self.hass,
                self.coordinator.config_entry.entry_id,
                self._kind,
                self._node_name,
                self.device_id,
                snapshot_name,
                result,
                package_truth_invalidated=package_truth_invalidated,
            )
        except Exception:
            _LOGGER.exception(
                "Could not publish snapshot Restore result for %s/%s",
                self._node_name,
                self.device_id,
            )
        if cancelled:
            self.hass.async_create_task(
                self.coordinator.async_request_refresh(),
                f"snapshot Restore refresh {self._node_name}/{self.device_id}",
            )
            raise asyncio.CancelledError
        await self.coordinator.async_request_refresh()

    @property
    @override
    def available(self) -> bool:
        """Reflect target, permission, and LXC exclusion state."""
        return (
            super().available
            and _has_restore_permissions(self.coordinator, self.device_id)
            and (
                self._kind is SnapshotKind.QEMU
                or not self.coordinator.package_manager.restore_reserved(
                    self._node_name, self.device_id
                )
            )
        )


class ProxmoxVMSnapshotRestoreButton(SnapshotRestoreButtonMixin, ProxmoxVMEntity):
    """Explicit QEMU native snapshot Restore button."""

    _kind = SnapshotKind.QEMU

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        vm_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize one QEMU Restore button."""
        super().__init__(coordinator, SNAPSHOT_RESTORE_BUTTON, vm_data, node_data)


class ProxmoxContainerSnapshotRestoreButton(
    SnapshotRestoreButtonMixin, ProxmoxContainerEntity
):
    """Explicit LXC native snapshot Restore button."""

    _kind = SnapshotKind.LXC

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        container_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize one LXC Restore button."""
        super().__init__(
            coordinator, SNAPSHOT_RESTORE_BUTTON, container_data, node_data
        )


class PackageScanButtonEntity(ProxmoxContainerEntity, ProxmoxBaseButton):
    """Manually scan pending packages for an upstream-discovered LXC."""

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        container_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize the package scan button."""
        super().__init__(coordinator, PACKAGE_SCAN_BUTTON, container_data, node_data)

    @override
    async def async_press(self) -> None:
        """Start the package scan and translate a synchronous rejection."""
        try:
            await self._async_press_call()
        except PackageScanError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="package_scan_failed",
                translation_placeholders={"reason": str(err)},
            ) from err

    @override
    async def _async_press_call(self) -> None:
        """Start a tracked scan without occupying the native button semaphore."""
        node_data = self.coordinator.data.get(self._node_name)
        container = (
            node_data.containers.get(self.device_id) if node_data is not None else None
        )
        self.coordinator.package_manager.async_start_scan(
            self._node_name,
            self.device_id,
            target_is_running=(
                container is not None
                and container.get("status") == VM_CONTAINER_RUNNING
            ),
        )

    @property
    @override
    def available(self) -> bool:
        """Return whether current upstream state says this LXC can be scanned."""
        manager = self.coordinator.package_manager
        scan = manager.record(self._node_name, self.device_id)
        update = manager.update_record(self._node_name, self.device_id)
        cleanup = manager.cleanup_record(self._node_name, self.device_id)
        return (
            super().available
            and self.container_data.get("status") == VM_CONTAINER_RUNNING
            and not manager.restore_reserved(self._node_name, self.device_id)
            and scan.status is not PackageScanStatus.RUNNING
            and update.status is not PackageUpdateStatus.RUNNING
            and cleanup.status is not PackageUpdateStatus.RUNNING
        )


class PackageReviewButtonEntity(ProxmoxContainerEntity, ButtonEntity):
    """Render the exact current package plan for operator review."""

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        container_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize the package review button."""
        super().__init__(coordinator, PACKAGE_REVIEW_BUTTON, container_data, node_data)

    @override
    async def async_press(self) -> None:
        """Show the full current plan, then remember exactly its scan token."""
        record = self.coordinator.package_manager.record(
            self._node_name, self.device_id
        )
        if (
            record.status is not PackageScanStatus.SUCCESS
            or record.result is None
            or not record.result.packages
            or record.token is None
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="package_review_failed",
            )
        notify_review_plan(
            self.hass, self._node_name, self.device_id, record
        )
        if not self.coordinator.package_manager.mark_viewed(
            self._node_name, self.device_id, record.token
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="package_review_failed",
            )

    @property
    @override
    def available(self) -> bool:
        """Return whether a successful non-empty plan can currently be viewed."""
        manager = self.coordinator.package_manager
        record = manager.record(self._node_name, self.device_id)
        update = manager.update_record(self._node_name, self.device_id)
        cleanup = manager.cleanup_record(self._node_name, self.device_id)
        return (
            super().available
            and self.container_data.get("status") == VM_CONTAINER_RUNNING
            and not manager.restore_reserved(self._node_name, self.device_id)
            and update.status is not PackageUpdateStatus.RUNNING
            and cleanup.status is not PackageUpdateStatus.RUNNING
            and record.status is PackageScanStatus.SUCCESS
            and record.result is not None
            and bool(record.result.packages)
        )


class PackageApproveButtonEntity(ProxmoxContainerEntity, ButtonEntity):
    """Approve exactly the plan token most recently rendered by Review."""

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        container_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize the package approval button."""
        super().__init__(coordinator, PACKAGE_APPROVE_BUTTON, container_data, node_data)

    @override
    async def async_press(self) -> None:
        """Confirm exactly the ephemeral viewed token through existing logic."""
        if not self.coordinator.package_manager.confirm_viewed_review(
            self._node_name, self.device_id
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="package_approval_failed",
            )

    @property
    @override
    def available(self) -> bool:
        """Return whether the exact current plan was viewed but not approved."""
        manager = self.coordinator.package_manager
        record = manager.record(self._node_name, self.device_id)
        update = manager.update_record(self._node_name, self.device_id)
        cleanup = manager.cleanup_record(self._node_name, self.device_id)
        return (
            super().available
            and self.container_data.get("status") == VM_CONTAINER_RUNNING
            and not manager.restore_reserved(self._node_name, self.device_id)
            and update.status is not PackageUpdateStatus.RUNNING
            and cleanup.status is not PackageUpdateStatus.RUNNING
            and record.status is PackageScanStatus.SUCCESS
            and record.result is not None
            and bool(record.result.packages)
            and not record.reviewed
            and record.token is not None
            and manager.viewed_token(self._node_name, self.device_id) == record.token
        )


class PackageUpdateButtonEntity(ProxmoxContainerEntity, ButtonEntity):
    """Start one explicitly approved native-snapshot-protected package update."""

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        container_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize the package update button."""
        super().__init__(coordinator, PACKAGE_UPDATE_BUTTON, container_data, node_data)

    @override
    async def async_press(self) -> None:
        """Start a background update after independently revalidating state."""
        node_data = self.coordinator.data.get(self._node_name)
        container = (
            node_data.containers.get(self.device_id) if node_data is not None else None
        )
        try:
            self.coordinator.package_manager.async_start_update(
                self._node_name,
                self.device_id,
                target_is_running=(
                    container is not None
                    and container.get("status") == VM_CONTAINER_RUNNING
                ),
                snapshot_permission=is_granted(
                    self.coordinator.permissions,
                    p_type="vms",
                    p_id=self.device_id,
                    permission=ProxmoxPermission.SNAPSHOT,
                ),
            )
        except PackageUpdateError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="package_update_failed",
                translation_placeholders={"reason": str(err)},
            ) from err

    @property
    @override
    def available(self) -> bool:
        """Return whether one current non-empty plan is approved for update."""
        manager = self.coordinator.package_manager
        record = manager.record(self._node_name, self.device_id)
        update = manager.update_record(self._node_name, self.device_id)
        cleanup = manager.cleanup_record(self._node_name, self.device_id)
        return (
            super().available
            and self.container_data.get("status") == VM_CONTAINER_RUNNING
            and not manager.restore_reserved(self._node_name, self.device_id)
            and update.status is not PackageUpdateStatus.RUNNING
            and cleanup.status is not PackageUpdateStatus.RUNNING
            and record.status is PackageScanStatus.SUCCESS
            and record.result is not None
            and bool(record.result.packages)
            and record.reviewed
            and is_granted(
                self.coordinator.permissions,
                p_type="vms",
                p_id=self.device_id,
                permission=ProxmoxPermission.SNAPSHOT,
            )
        )


class PackageAutoremoveButtonEntity(ProxmoxContainerEntity, ButtonEntity):
    """Run only the exact displayed and freshly revalidated cleanup plan."""

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        container_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize the explicit Autoremove button."""
        super().__init__(
            coordinator, PACKAGE_AUTOREMOVE_BUTTON, container_data, node_data
        )

    @override
    async def async_press(self) -> None:
        """Start explicit cleanup after independently revalidating current state."""
        node_data = self.coordinator.data.get(self._node_name)
        container = (
            node_data.containers.get(self.device_id) if node_data is not None else None
        )
        try:
            self.coordinator.package_manager.async_start_autoremove(
                self._node_name,
                self.device_id,
                target_is_running=(
                    container is not None
                    and container.get("status") == VM_CONTAINER_RUNNING
                ),
                snapshot_permission=is_granted(
                    self.coordinator.permissions,
                    p_type="vms",
                    p_id=self.device_id,
                    permission=ProxmoxPermission.SNAPSHOT,
                ),
            )
        except PackageUpdateError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="package_autoremove_failed",
                translation_placeholders={"reason": str(err)},
            ) from err

    @property
    @override
    def available(self) -> bool:
        """Require current displayed evidence and no conflicting package flow."""
        manager = self.coordinator.package_manager
        scan = manager.record(self._node_name, self.device_id)
        update = manager.update_record(self._node_name, self.device_id)
        cleanup = manager.cleanup_record(self._node_name, self.device_id)
        evidence = manager.cleanup_evidence(self._node_name, self.device_id)
        return (
            super().available
            and self.container_data.get("status") == VM_CONTAINER_RUNNING
            and not manager.restore_reserved(self._node_name, self.device_id)
            and scan.status is not PackageScanStatus.RUNNING
            and update.status is not PackageUpdateStatus.RUNNING
            and cleanup.status is not PackageUpdateStatus.RUNNING
            and evidence is not None
            and bool(evidence.candidates)
            and is_granted(
                self.coordinator.permissions,
                p_type="vms",
                p_id=self.device_id,
                permission=ProxmoxPermission.SNAPSHOT,
            )
        )
