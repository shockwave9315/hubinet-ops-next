"""Package-scan orchestration, concurrency, and ephemeral state."""

import asyncio
from collections.abc import Callable, Collection
from dataclasses import replace
from datetime import UTC, datetime
import logging
import secrets
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
_SCAN_TOKEN_BYTES = 16


def _generate_scan_token() -> str:
    """Return one fresh ~128-bit opaque scan token for a successful scan.

    The token is optimistic-concurrency metadata only: it is not a secret,
    not authentication, not a resource identity, and encodes no node,
    VMID, or config-entry data. Every successful scan gets a new one, even
    when the observed package rows are unchanged.
    """
    return secrets.token_urlsafe(_SCAN_TOKEN_BYTES)


class PackageTransport(Protocol):
    """Transport boundary required by the package manager."""

    @property
    def configured(self) -> bool:
        """Return whether required local transport material is present."""

    async def async_prepare(self, hass: HomeAssistant) -> None:
        """Evaluate and cache local transport prerequisites once, off the loop."""

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
        self._tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._scan_slots = asyncio.Semaphore(_MAX_GLOBAL_SCANS)

    @property
    def configured(self) -> bool:
        """Return whether package entities can perform their transport."""
        return self._transport.configured

    async def async_prepare(self) -> None:
        """Load package SSH trust material once for this config-entry setup."""
        await self._transport.async_prepare(self._hass)

    def record(self, node: str, vmid: int) -> PackageScanRecord:
        """Return current ephemeral scan state for one upstream identity."""
        return self._records.get((node, vmid), PackageScanRecord())

    @callback
    def async_prune(self, current_targets: Collection[tuple[str, int]]) -> None:
        """Discard scan state for VMIDs no longer present upstream.

        Deleting the record alone would not stop a stale in-flight scan for
        a removed VMID from completing later and recreating the pruned
        result; :meth:`_async_run_scan` only ever writes back to the record
        object it started with, so an attempt for a pruned (or since
        reused) target can never resurrect stale evidence.
        """
        stale = [key for key in self._records if key not in current_targets]
        if not stale:
            return
        for key in stale:
            del self._records[key]
            task = self._tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
        self._on_state_change()

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
        own_record = PackageScanRecord(
            status=PackageScanStatus.RUNNING,
            last_attempt=attempted_at,
        )
        self._set_record(node, vmid, own_record)
        task = self._config_entry.async_create_background_task(
            self._hass,
            self._async_run_scan(node, vmid, attempted_at, own_record),
            f"package scan {node}/{vmid}",
        )
        self._tasks[(node, vmid)] = task
        return task

    async def _async_run_scan(
        self,
        node: str,
        vmid: int,
        attempted_at: datetime,
        own_record: PackageScanRecord,
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
            outcome = PackageScanRecord(
                status=PackageScanStatus.FAILED,
                last_attempt=attempted_at,
                failure=err.failure,
                error_message=str(err)[:_MAX_ERROR_MESSAGE_LENGTH],
            )
        except Exception:
            _LOGGER.exception("Unexpected package scan failure for %s/%s", node, vmid)
            outcome = PackageScanRecord(
                status=PackageScanStatus.FAILED,
                last_attempt=attempted_at,
                failure=PackageScanFailure.EXECUTION_FAILED,
                error_message="unexpected package scan failure",
            )
        else:
            outcome = PackageScanRecord(
                status=PackageScanStatus.SUCCESS,
                last_attempt=attempted_at,
                result=result,
                token=_generate_scan_token(),
            )
        self._finish_scan(node, vmid, own_record, outcome)

    @callback
    def _finish_scan(
        self,
        node: str,
        vmid: int,
        own_record: PackageScanRecord,
        outcome: PackageScanRecord,
    ) -> None:
        """Write this attempt's outcome only if it is still the current one.

        A target pruned by :meth:`async_prune` (VMID deleted, or reused by
        a different container) replaces or removes the record this attempt
        started with; an identity check against the exact record object
        prevents a stale in-flight completion from resurrecting it.
        """
        if self._records.get((node, vmid)) is not own_record:
            return
        self._tasks.pop((node, vmid), None)
        self._set_record(node, vmid, outcome)

    @callback
    def confirm_review(self, node: str, vmid: int, token: str) -> bool:
        """Confirm the current successful scan's plan as reviewed.

        Confirmation can only ever act on a stored ``SUCCESS`` record: a
        scan in flight for this target owns a ``RUNNING`` record instead,
        so this can never target (or block) an in-flight attempt -- a
        confirmation racing a scan either sees the old successful record
        (rejected once the new one lands, since the token then differs) or
        the new one once it exists. Reading the current record, validating
        its token, and replacing it happen here with no ``await`` between
        them, so nothing can interleave a scan completion in between.

        Returns ``True`` once ``reviewed`` is set, and ``False`` for every
        expected business rejection (no current record, a non-successful
        record, a missing or mismatched token, or an empty package plan) --
        never by raising.
        """
        record = self._records.get((node, vmid))
        if (
            record is None
            or record.status is not PackageScanStatus.SUCCESS
            or record.result is None
            or record.token is None
            or record.token != token
            or not record.result.packages
        ):
            return False
        self._set_record(node, vmid, replace(record, reviewed=True))
        return True

    @callback
    def _set_record(self, node: str, vmid: int, record: PackageScanRecord) -> None:
        """Atomically replace one record and notify summary entities."""
        self._records[(node, vmid)] = record
        self._on_state_change()
