"""Button platform for Proxmox VE."""

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from proxmoxer import AuthenticationError
from proxmoxer.core import ResourceException
import requests
from requests.exceptions import ConnectTimeout, SSLError

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
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
from .snapshot_restore import setup_restore_buttons

PARALLEL_UPDATES = 1


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ProxmoxVE buttons."""
    coordinator = entry.runtime_data
    setup_restore_buttons(coordinator, async_add_entities)

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
        async_add_entities(
            ProxmoxVMButtonEntity(coordinator, entity_description, vm, node_data)
            for (node_data, vm) in vms
            for entity_description in VM_BUTTONS
            if is_granted(
                coordinator.permissions,
                p_type=entity_description.permission_target,
                p_id=vm["vmid"],
                permission=entity_description.permission,
            )
        )

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
