"""Sensor platform for Proxmox VE integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import IntFlag
from typing import Any, override

import voluptuous as vol

from homeassistant.components.sensor import (
    EntityCategory,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
    StateType,
)
from homeassistant.const import PERCENTAGE, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import VM_CONTAINER_RUNNING, ProxmoxPermission
from .coordinator import ProxmoxConfigEntry, ProxmoxCoordinator, ProxmoxNodeData
from .entity import (
    ProxmoxContainerEntity,
    ProxmoxNodeEntity,
    ProxmoxStorageEntity,
    ProxmoxVMEntity,
)
from .helpers import is_granted
from .packages.models import PackageScanRecord, PackageScanStatus

PARALLEL_UPDATES = 0

SERVICE_GET_PACKAGE_PLAN = "get_package_plan"
SERVICE_CONFIRM_PACKAGE_REVIEW = "confirm_package_review"
ATTR_TOKEN = "token"


class PackageReviewEntityFeature(IntFlag):
    """Supported features for the package-review entity actions.

    This bit exists only so the two package-review entity actions can be
    registered with native ``required_features`` filtering. Registering an
    entity service from this platform module would otherwise make it
    eligible on every Hubinet-Ops sensor sharing the ``sensor`` platform,
    not only :class:`PackageScanSensor`.
    """

    REVIEW = 1


@dataclass(frozen=True, kw_only=True)
class ProxmoxNodeSensorEntityDescription(SensorEntityDescription):
    """Class to hold Proxmox node sensor description."""

    value_fn: Callable[[ProxmoxNodeData], StateType | datetime]
    permission: ProxmoxPermission = ProxmoxPermission.SYSAUDIT
    permission_target: str = "nodes"


@dataclass(frozen=True, kw_only=True)
class ProxmoxVMSensorEntityDescription(SensorEntityDescription):
    """Class to hold Proxmox VM sensor description."""

    value_fn: Callable[[dict[str, Any]], StateType]


@dataclass(frozen=True, kw_only=True)
class ProxmoxContainerSensorEntityDescription(SensorEntityDescription):
    """Class to hold Proxmox container sensor description."""

    value_fn: Callable[[dict[str, Any]], StateType]


@dataclass(frozen=True, kw_only=True)
class ProxmoxStorageSensorEntityDescription(SensorEntityDescription):
    """Class to hold Proxmox storage sensor description."""

    value_fn: Callable[[dict[str, Any]], StateType]


NODE_SENSORS: tuple[ProxmoxNodeSensorEntityDescription, ...] = (
    ProxmoxNodeSensorEntityDescription(
        key="node_cpu",
        translation_key="node_cpu",
        value_fn=lambda data: data.node["cpu"] * 100,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_max_cpu",
        translation_key="node_max_cpu",
        value_fn=lambda data: data.node["maxcpu"],
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_disk",
        translation_key="node_disk",
        value_fn=lambda data: data.node["disk"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_max_disk",
        translation_key="node_max_disk",
        value_fn=lambda data: data.node["maxdisk"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_memory",
        translation_key="node_memory",
        value_fn=lambda data: data.node["mem"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_max_memory",
        translation_key="node_max_memory",
        value_fn=lambda data: data.node["maxmem"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_memory_percentage",
        translation_key="node_memory_percentage",
        value_fn=lambda data: int(data.node["mem"]) / int(data.node["maxmem"]) * 100,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_uptime",
        translation_key="node_uptime",
        value_fn=lambda data: data.node["uptime"],
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_status",
        translation_key="node_status",
        value_fn=lambda data: data.node["status"],
        device_class=SensorDeviceClass.ENUM,
        options=["online", "offline"],
        permission=ProxmoxPermission.VMAUDIT,
        permission_target="vms",
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_backup_last_backup",
        translation_key="node_backup_last_backup",
        value_fn=lambda data: (
            dt_util.utc_from_timestamp(data.backups[0]["endtime"])
            if data.backups
            else None
        ),
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ProxmoxNodeSensorEntityDescription(
        key="node_backup_duration",
        translation_key="node_backup_duration",
        value_fn=lambda data: (
            data.backups[0]["endtime"] - data.backups[0]["starttime"]
            if data.backups
            else None
        ),
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
)

VM_SENSORS: tuple[ProxmoxVMSensorEntityDescription, ...] = (
    ProxmoxVMSensorEntityDescription(
        key="vm_max_cpu",
        translation_key="vm_max_cpu",
        value_fn=lambda data: data["cpus"],
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_cpu",
        translation_key="vm_cpu",
        value_fn=lambda data: data["cpu"] * 100,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_memory",
        translation_key="vm_memory",
        value_fn=lambda data: data["mem"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_max_memory",
        translation_key="vm_max_memory",
        value_fn=lambda data: data["maxmem"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_memory_percentage",
        translation_key="vm_memory_percentage",
        value_fn=lambda data: int(data["mem"]) / int(data["maxmem"]) * 100,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_uptime",
        translation_key="vm_uptime",
        value_fn=lambda data: data.get("uptime"),
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_disk",
        translation_key="vm_disk",
        value_fn=lambda data: data["disk"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_max_disk",
        translation_key="vm_max_disk",
        value_fn=lambda data: data["maxdisk"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_status",
        translation_key="vm_status",
        value_fn=lambda data: data["status"],
        device_class=SensorDeviceClass.ENUM,
        options=["running", "stopped", "suspended"],
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_netin",
        translation_key="vm_netin",
        value_fn=lambda data: data["netin"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
    ),
    ProxmoxVMSensorEntityDescription(
        key="vm_netout",
        translation_key="vm_netout",
        value_fn=lambda data: data["netout"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
    ),
)

CONTAINER_SENSORS: tuple[ProxmoxContainerSensorEntityDescription, ...] = (
    ProxmoxContainerSensorEntityDescription(
        key="container_max_cpu",
        translation_key="container_max_cpu",
        value_fn=lambda data: data["cpus"],
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_cpu",
        translation_key="container_cpu",
        value_fn=lambda data: data["cpu"] * 100,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_memory",
        translation_key="container_memory",
        value_fn=lambda data: data["mem"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_max_memory",
        translation_key="container_max_memory",
        value_fn=lambda data: data["maxmem"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_memory_percentage",
        translation_key="container_memory_percentage",
        value_fn=lambda data: int(data["mem"]) / int(data["maxmem"]) * 100,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_uptime",
        translation_key="container_uptime",
        value_fn=lambda data: data.get("uptime"),
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_disk",
        translation_key="container_disk",
        value_fn=lambda data: data["disk"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_max_disk",
        translation_key="container_max_disk",
        value_fn=lambda data: data["maxdisk"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_status",
        translation_key="container_status",
        value_fn=lambda data: data["status"],
        device_class=SensorDeviceClass.ENUM,
        options=["running", "stopped", "suspended"],
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_netin",
        translation_key="container_netin",
        value_fn=lambda data: data["netin"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
    ),
    ProxmoxContainerSensorEntityDescription(
        key="container_netout",
        translation_key="container_netout",
        value_fn=lambda data: data["netout"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
    ),
)

STORAGE_SENSORS: tuple[ProxmoxStorageSensorEntityDescription, ...] = (
    ProxmoxStorageSensorEntityDescription(
        key="storage_used",
        translation_key="storage_used",
        value_fn=lambda data: data["used"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxStorageSensorEntityDescription(
        key="storage_total",
        translation_key="storage_total",
        value_fn=lambda data: data["total"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxStorageSensorEntityDescription(
        key="storage_available",
        translation_key="storage_available",
        value_fn=lambda data: data["avail"],
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ProxmoxStorageSensorEntityDescription(
        key="storage_used_percentage",
        translation_key="storage_used_percentage",
        value_fn=lambda data: (
            round(value * 100, 1)
            if (value := data.get("used_fraction")) is not None
            else None
        ),
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)

PACKAGE_SCAN_SENSOR = SensorEntityDescription(
    key="pending_packages",
    translation_key="pending_packages",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ProxmoxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Proxmox VE sensors."""
    coordinator = entry.runtime_data

    def _async_add_new_nodes(nodes: list[ProxmoxNodeData]) -> None:
        """Add new node sensors."""
        async_add_entities(
            ProxmoxNodeSensor(coordinator, entity_description, node)
            for node in nodes
            for entity_description in NODE_SENSORS
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
        """Add new VM sensors."""
        async_add_entities(
            ProxmoxVMSensor(coordinator, entity_description, vm, node_data)
            for (node_data, vm) in vms
            for entity_description in VM_SENSORS
        )

    def _async_add_new_containers(
        containers: list[tuple[ProxmoxNodeData, dict[str, Any]]],
    ) -> None:
        """Add new container sensors."""
        entities: list[SensorEntity] = [
            ProxmoxContainerSensor(
                coordinator, entity_description, container, node_data
            )
            for (node_data, container) in containers
            for entity_description in CONTAINER_SENSORS
        ]
        if coordinator.package_manager.configured:
            entities.extend(
                PackageScanSensor(coordinator, container, node_data)
                for node_data, container in containers
            )
        async_add_entities(entities)

    def _async_add_new_storages(
        storages: list[tuple[ProxmoxNodeData, dict[str, Any]]],
    ) -> None:
        """Add new storage sensors."""
        async_add_entities(
            ProxmoxStorageSensor(coordinator, entity_description, storage, node_data)
            for (node_data, storage) in storages
            for entity_description in STORAGE_SENSORS
        )

    coordinator.new_nodes_callbacks.append(_async_add_new_nodes)
    coordinator.new_vms_callbacks.append(_async_add_new_vms)
    coordinator.new_containers_callbacks.append(_async_add_new_containers)
    coordinator.new_storages_callbacks.append(_async_add_new_storages)

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
    _async_add_new_storages(
        [
            (node_data, storage_data)
            for node_data in coordinator.data.values()
            for storage_name, storage_data in node_data.storages.items()
            if (node_data.node["node"], storage_name) in coordinator.known_storages
        ]
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_GET_PACKAGE_PLAN,
        None,
        "async_get_package_plan",
        required_features=[PackageReviewEntityFeature.REVIEW],
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_CONFIRM_PACKAGE_REVIEW,
        {vol.Required(ATTR_TOKEN): cv.string},
        "async_confirm_package_review",
        required_features=[PackageReviewEntityFeature.REVIEW],
        supports_response=SupportsResponse.ONLY,
    )


class ProxmoxNodeSensor(ProxmoxNodeEntity, SensorEntity):
    """Representation of a Proxmox VE node sensor."""

    entity_description: ProxmoxNodeSensorEntityDescription

    @property
    @override
    def native_value(self) -> StateType | datetime:
        """Return the native value of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data[self.device_name])


class ProxmoxVMSensor(ProxmoxVMEntity, SensorEntity):
    """Represents a Proxmox VE VM sensor."""

    entity_description: ProxmoxVMSensorEntityDescription

    @property
    @override
    def native_value(self) -> StateType:
        """Return the native value of the sensor."""
        return self.entity_description.value_fn(self.vm_data)


class ProxmoxContainerSensor(ProxmoxContainerEntity, SensorEntity):
    """Represents a Proxmox VE container sensor."""

    entity_description: ProxmoxContainerSensorEntityDescription

    @property
    @override
    def native_value(self) -> StateType:
        """Return the native value of the sensor."""
        return self.entity_description.value_fn(self.container_data)


class PackageScanSensor(ProxmoxContainerEntity, SensorEntity):
    """Last manually requested pending-package scan for one LXC."""

    _attr_supported_features = PackageReviewEntityFeature.REVIEW

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        container_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize the package scan sensor."""
        super().__init__(coordinator, PACKAGE_SCAN_SENSOR, container_data, node_data)

    @property
    def _scan_record(self) -> PackageScanRecord:
        return self.coordinator.package_manager.record(self._node_name, self.device_id)

    @property
    @override
    def available(self) -> bool:
        """Return whether current upstream state says this LXC can be scanned.

        A stored successful result is not equivalent to zero pending
        packages for a guest that is not currently running -- packages
        cannot be scanned (or have changed) while it is stopped, so the
        sensor is unavailable rather than exposing a possibly-stale exact
        count. The record itself is preserved and becomes visible again
        once the guest is running again.
        """
        return (
            super().available
            and self.container_data.get("status") == VM_CONTAINER_RUNNING
        )

    @property
    @override
    def native_value(self) -> int | None:
        """Return a count only when the latest attempt succeeded."""
        record = self._scan_record
        if record.status is not PackageScanStatus.SUCCESS or record.result is None:
            return None
        return len(record.result.packages)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return bounded status and summary evidence, never exact package rows."""
        record = self._scan_record
        attributes: dict[str, Any] = {
            "scan_status": record.status,
            "running": record.status is PackageScanStatus.RUNNING,
        }
        if record.last_attempt is not None:
            attributes["last_attempt"] = record.last_attempt.isoformat()
        if record.status is PackageScanStatus.FAILED:
            attributes["last_error"] = record.failure
            attributes["last_error_message"] = record.error_message
        elif record.status is PackageScanStatus.SUCCESS and record.result is not None:
            result = record.result
            attributes.update(
                {
                    "os_id": result.os_id,
                    "os_version": result.os_version,
                    "reboot_required": result.reboot_required,
                    "security_updates": sum(
                        package.security is True for package in result.packages
                    ),
                    "unknown_security_updates": sum(
                        package.security is None for package in result.packages
                    ),
                    "not_upgraded_count": result.not_upgraded_count,
                    "reviewed": record.reviewed,
                }
            )
        return attributes

    async def async_get_package_plan(self) -> ServiceResponse:
        """Return the latest scan status and, for a success, its exact plan.

        This is read-only evidence: it never triggers a scan and never
        changes review state. The scan token and exact package rows are
        returned only for a successful scan, never placed in entity state
        or attributes.
        """
        record = self._scan_record
        if record.status is not PackageScanStatus.SUCCESS or record.result is None:
            return {"status": record.status}
        return {
            "status": record.status,
            "token": record.token,
            "reviewed": record.reviewed,
            "packages": [
                {
                    "name": package.name,
                    "architecture": package.architecture,
                    "installed_version": package.installed_version,
                    "candidate_version": package.candidate_version,
                    "origin": package.origin,
                    "security": package.security,
                }
                for package in record.result.packages
            ],
        }

    async def async_confirm_package_review(self, token: str) -> ServiceResponse:
        """Confirm the plan behind ``token`` as reviewed, or reject it.

        A stale/wrong token, no current successful scan, and an empty plan
        are expected business outcomes returned as ``{"reviewed": False}``,
        never raised -- an entity-service call may target several package
        sensors at once, and one target's rejection must not fail another
        target's already-succeeded confirmation.
        """
        confirmed = self.coordinator.package_manager.confirm_review(
            self._node_name, self.device_id, token
        )
        return {"reviewed": confirmed}


class ProxmoxStorageSensor(ProxmoxStorageEntity, SensorEntity):
    """Represents a Proxmox VE storage sensor."""

    entity_description: ProxmoxStorageSensorEntityDescription

    @property
    @override
    def native_value(self) -> StateType:
        """Return the native value of the sensor."""
        return self.entity_description.value_fn(self.storage_data)
