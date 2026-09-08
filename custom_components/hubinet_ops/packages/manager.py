"""Package-scan orchestration, concurrency, and ephemeral state."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import logging
from typing import Any, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .models import (
    PackageScanError,
    PackageScanFailure,
    PackageScanRecord,
    PackageScanResult,
    PackageScanStatus,
)

_LOGGER = logging.getLogger(__name__)

_MAX_GLOBAL_SCANS = 2
_MAX_ERROR_MESSAGE_LENGTH = 500


class PackageTransport(Protocol):
    """Transport boundary required by the package manager."""

    @property
    def configured(self) -> bool:
        """Return whether required local transport material is present."""

    async def async_scan(self, expected_node: str, vmid: int) -> PackageScanResult:
        """Return fresh scan evidence for one target."""


def _utcnow() -> datetime:
    """Return an aware timestamp for scan records."""
    return datetime.now(UTC)


class PackageManager:
    """Own package scan state and concurrency outside the PVE coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry[Any],
        *,
        transport: PackageTransport,
        on_state_change: Callable[[], None],
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Initialize the package subsystem boundary."""
        self._hass = hass
        self._config_entry = config_entry
        self._transport = transport
        self._on_state_change = on_state_change
        self._now = now
        self._records: dict[tuple[str, int], PackageScanRecord] = {}
        self._scan_slots = asyncio.Semaphore(_MAX_GLOBAL_SCANS)

    @property
    def configured(self) -> bool:
        """Return whether package entities can perform their transport."""
        return self._transport.configured

    def record(self, node: str, vmid: int) -> PackageScanRecord:
        """Return current ephemeral scan state for one upstream identity."""
        return self._records.get((node, vmid), PackageScanRecord())

    @callback
    def async_start_scan(
        self, node: str, vmid: int, *, target_is_running: bool
    ) -> asyncio.Task[None]:
        """Start a lifecycle-tracked scan and return without waiting for SSH."""
        if not self.configured:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan SSH trust material is not configured",
            )
        if not target_is_running:
            raise PackageScanError(
                PackageScanFailure.GUEST_UNAVAILABLE,
                "LXC is not present and running in current Proxmox data",
            )
        if any(
            target_vmid == vmid and record.status is PackageScanStatus.RUNNING
            for (_node, target_vmid), record in self._records.items()
        ):
            raise PackageScanError(
                PackageScanFailure.PACKAGE_MANAGER_BUSY,
                "a package scan is already running for this LXC VMID",
            )

        attempted_at = self._now()
        self._set_record(
            node,
            vmid,
            PackageScanRecord(
                status=PackageScanStatus.RUNNING,
                last_attempt=attempted_at,
            ),
        )
        return self._config_entry.async_create_background_task(
            self._hass,
            self._async_run_scan(node, vmid, attempted_at),
            f"package scan {node}/{vmid}",
        )

    async def _async_run_scan(
        self, node: str, vmid: int, attempted_at: datetime
    ) -> None:
        """Run one bounded scan and capture every outcome into scan state."""
        try:
            async with self._scan_slots:
                result = await self._transport.async_scan(node, vmid)
        except PackageScanError as err:
            _LOGGER.warning(
                "Package scan for %s/%s failed: %s (%s)",
                node,
                vmid,
                err.failure,
                err,
            )
            self._set_record(
                node,
                vmid,
                PackageScanRecord(
                    status=PackageScanStatus.FAILED,
                    last_attempt=attempted_at,
                    failure=err.failure,
                    error_message=str(err)[:_MAX_ERROR_MESSAGE_LENGTH],
                ),
            )
        except Exception:
            _LOGGER.exception("Unexpected package scan failure for %s/%s", node, vmid)
            self._set_record(
                node,
                vmid,
                PackageScanRecord(
                    status=PackageScanStatus.FAILED,
                    last_attempt=attempted_at,
                    failure=PackageScanFailure.EXECUTION_FAILED,
                    error_message="unexpected package scan failure",
                ),
            )
        else:
            self._set_record(
                node,
                vmid,
                PackageScanRecord(
                    status=PackageScanStatus.SUCCESS,
                    last_attempt=attempted_at,
                    result=result,
                ),
            )

    @callback
    def _set_record(self, node: str, vmid: int, record: PackageScanRecord) -> None:
        """Atomically replace one record and notify summary entities."""
        self._records[(node, vmid)] = record
        self._on_state_change()
