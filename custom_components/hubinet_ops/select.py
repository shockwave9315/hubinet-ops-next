"""Presentation-only native snapshot selection for Proxmox VE guests."""

from datetime import timedelta
import logging
from typing import Any, override

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, ProxmoxPermission
from .coordinator import ProxmoxConfigEntry, ProxmoxCoordinator, ProxmoxNodeData
from .entity import ProxmoxContainerEntity, ProxmoxVMEntity
from .helpers import is_granted
from .snapshots import SnapshotKind, SnapshotListError, async_list_snapshots

SCAN_INTERVAL = timedelta(seconds=300)
ATTR_SELECTED_SNAPSHOT = "selected_snapshot"

SNAPSHOT_SELECT = SelectEntityDescription(
    key="snapshot_to_restore",
    translation_key="snapshot_to_restore",
    entity_category=EntityCategory.CONFIG,
)

_LOGGER = logging.getLogger(__name__)


def snapshot_select_unique_id(entry_id: str, vmid: int) -> str:
    """Return the stable selection entity unique ID for one exact guest."""
    return f"{entry_id}_{vmid}_{SNAPSHOT_SELECT.key}"


def snapshot_selection_signal(
    entry_id: str, kind: SnapshotKind, node: str, vmid: int
) -> str:
    """Return an entry-and-target-scoped selection-consumption signal."""
    return f"{DOMAIN}_snapshot_selection_clear_{entry_id}_{kind}_{node}_{vmid}"


def has_restore_permissions(
    coordinator: ProxmoxCoordinator, vmid: int
) -> bool:
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
    """Set up independently polling native snapshot selectors."""
    coordinator = entry.runtime_data

    def _async_add_new_vms(
        vms: list[tuple[ProxmoxNodeData, dict[str, Any]]],
    ) -> None:
        async_add_entities(
            (
                ProxmoxVMSnapshotSelect(coordinator, vm, node_data)
                for node_data, vm in vms
                if has_restore_permissions(coordinator, int(vm["vmid"]))
            ),
        )

    def _async_add_new_containers(
        containers: list[tuple[ProxmoxNodeData, dict[str, Any]]],
    ) -> None:
        async_add_entities(
            (
                ProxmoxContainerSnapshotSelect(coordinator, container, node_data)
                for node_data, container in containers
                if has_restore_permissions(coordinator, int(container["vmid"]))
            ),
        )

    coordinator.new_vms_callbacks.append(_async_add_new_vms)
    coordinator.new_containers_callbacks.append(_async_add_new_containers)
    _async_add_new_vms(
        [
            (node_data, vm)
            for node_data in coordinator.data.values()
            for vmid, vm in node_data.vms.items()
            if (node_data.node["node"], vmid) in coordinator.known_vms
        ]
    )
    _async_add_new_containers(
        [
            (node_data, container)
            for node_data in coordinator.data.values()
            for vmid, container in node_data.containers.items()
            if (node_data.node["node"], vmid) in coordinator.known_containers
        ]
    )


class SnapshotSelectMixin(SelectEntity):
    """Own only one operator's ephemeral native snapshot choice."""

    _attr_options: list[str]
    _kind: SnapshotKind
    _node_name: str
    coordinator: ProxmoxCoordinator
    device_id: int

    def _init_snapshot_select(self) -> None:
        """Initialize state not owned by the upstream-derived base entity."""
        self._attr_unique_id = snapshot_select_unique_id(
            self.coordinator.config_entry.entry_id,
            self.device_id,
        )
        self._attr_options = []
        self._attr_current_option = None
        self._snapshot_available = False

    @property
    @override
    def should_poll(self) -> bool:
        """Poll this entity independently of its inherited coordinator."""
        return True

    async def async_added_to_hass(self) -> None:
        """Listen only for this selector's accepted Restore consumption."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                snapshot_selection_signal(
                    self.coordinator.config_entry.entry_id,
                    self._kind,
                    self._node_name,
                    self.device_id,
                ),
                self._async_clear_selection,
            )
        )
        self.async_schedule_update_ha_state(True)

    @callback
    def _async_clear_selection(self) -> None:
        """Consume the presentation choice without touching native PVE state."""
        self._attr_current_option = None
        self.async_write_ha_state()

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose collision-safe exact selection identity for Restore only."""
        selected = self._attr_current_option
        return {
            ATTR_SELECTED_SNAPSHOT: (
                selected
                if self._snapshot_available and selected in self._attr_options
                else None
            )
        }

    @property
    @override
    def available(self) -> bool:
        """Keep optional snapshot endpoint availability local to this entity."""
        node_data = self.coordinator.data.get(self._node_name)
        target_exists = node_data is not None and self.device_id in (
            node_data.vms if self._kind is SnapshotKind.QEMU else node_data.containers
        )
        return self._snapshot_available and target_exists

    @override
    async def async_update(self) -> None:
        """Poll only this guest's native snapshot endpoint."""
        try:
            snapshots = await async_list_snapshots(
                self.coordinator.proxmox,
                self._node_name,
                self.device_id,
                self._kind,
                executor=self.hass.async_add_executor_job,
            )
        except SnapshotListError:
            self._snapshot_available = False
            _LOGGER.debug(
                "Could not update native snapshot choices for %s/%s",
                self._node_name,
                self.device_id,
                exc_info=True,
            )
            return
        self._snapshot_available = True
        self._attr_options = [snapshot.name for snapshot in snapshots]
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = None

    @override
    async def async_select_option(self, option: str) -> None:
        """Remember only an exact option from the latest successful listing."""
        if not self._snapshot_available or option not in self._attr_options:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="snapshot_selection_invalid",
            )
        self._attr_current_option = option
        self.async_write_ha_state()


class ProxmoxVMSnapshotSelect(SnapshotSelectMixin, ProxmoxVMEntity):
    """Native QEMU snapshot choice."""

    _kind = SnapshotKind.QEMU

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        vm_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize one QEMU selector."""
        super().__init__(coordinator, SNAPSHOT_SELECT, vm_data, node_data)
        self._init_snapshot_select()


class ProxmoxContainerSnapshotSelect(SnapshotSelectMixin, ProxmoxContainerEntity):
    """Native LXC snapshot choice."""

    _kind = SnapshotKind.LXC

    def __init__(
        self,
        coordinator: ProxmoxCoordinator,
        container_data: dict[str, Any],
        node_data: ProxmoxNodeData,
    ) -> None:
        """Initialize one LXC selector."""
        super().__init__(coordinator, SNAPSHOT_SELECT, container_data, node_data)
        self._init_snapshot_select()
