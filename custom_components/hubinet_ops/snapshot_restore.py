"""Explicit native snapshot Restore button orchestration."""

import asyncio
from hashlib import sha256
from html import escape
import logging
import re
from typing import Any, override

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.translation import async_get_cached_translations
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ProxmoxCoordinator, ProxmoxNodeData
from .entity import ProxmoxContainerEntity, ProxmoxVMEntity
from .packages.models import PackageUpdateError
from .select import (
    ATTR_SELECTED_SNAPSHOT,
    has_restore_permissions,
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

SNAPSHOT_RESTORE_BUTTON = ButtonEntityDescription(
    key="snapshot_restore",
    translation_key="snapshot_restore",
    entity_category=EntityCategory.CONFIG,
)

_LOGGER = logging.getLogger(__name__)


def setup_restore_buttons(
    coordinator: ProxmoxCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up fork-owned QEMU and LXC native Restore buttons."""

    def _async_add_new_vms(
        vms: list[tuple[ProxmoxNodeData, dict[str, Any]]],
    ) -> None:
        async_add_entities(
            ProxmoxVMSnapshotRestoreButton(coordinator, vm, node_data)
            for node_data, vm in vms
            if has_restore_permissions(coordinator, int(vm["vmid"]))
        )

    def _async_add_new_containers(
        containers: list[tuple[ProxmoxNodeData, dict[str, Any]]],
    ) -> None:
        async_add_entities(
            ProxmoxContainerSnapshotRestoreButton(
                coordinator, container, node_data
            )
            for node_data, container in containers
            if has_restore_permissions(coordinator, int(container["vmid"]))
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
        if not has_restore_permissions(self.coordinator, self.device_id):
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
            and has_restore_permissions(self.coordinator, self.device_id)
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
