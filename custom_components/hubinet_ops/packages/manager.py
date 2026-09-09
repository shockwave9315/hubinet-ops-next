"""Package-scan orchestration, concurrency, and ephemeral state."""

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import replace
from datetime import UTC, datetime
import logging
import secrets
from typing import Any, Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .models import (
    PackageMutationResult,
    PackageScanError,
    PackageScanFailure,
    PackageScanRecord,
    PackageScanResult,
    PackageScanStatus,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
    PendingPackage,
)
from .parser import ParsedAptSimulation
from .snapshots import (
    SnapshotError,
    async_create_snapshot,
    async_delete_snapshot,
    async_list_snapshots,
    generate_snapshot_name,
    retained_snapshot_names,
)

_LOGGER = logging.getLogger(__name__)

_MAX_GLOBAL_SCANS = 2
_MAX_ERROR_MESSAGE_LENGTH = 500
_SCAN_TOKEN_BYTES = 16
_LIVENESS_RETRY_SECONDS = 3.0

type ProxmoxGetter = Callable[[], Any]
type Sleeper = Callable[[float], Awaitable[None]]
type RetainedSnapshotCallback = Callable[[str, int, tuple[str, ...]], None]
type UpdateCompleteCallback = Callable[[str, int, PackageUpdateRecord], None]


def _noop_retained(_node: str, _vmid: int, _names: tuple[str, ...]) -> None:
    """Default retained-snapshot callback."""


def _noop_complete(_node: str, _vmid: int, _record: PackageUpdateRecord) -> None:
    """Default update-completion callback."""


def package_plan_tuple(
    packages: Collection[PendingPackage],
) -> tuple[tuple[str, str, str, str], ...]:
    """Project only equality-bearing fields from a canonical package plan."""
    return tuple(
        (
            package.name,
            package.architecture,
            package.installed_version,
            package.candidate_version,
        )
        for package in packages
    )


def changed_package_count(result: PackageMutationResult) -> int:
    """Count actual installed-version row changes without making them a gate."""
    identities = result.before.keys() | result.after.keys()
    return sum(result.before.get(identity) != result.after.get(identity) for identity in identities)


def _require_plan_unchanged(
    fresh: ParsedAptSimulation,
    reviewed_plan: tuple[tuple[str, str, str, str], ...],
) -> None:
    """Stop before snapshot creation when equality-bearing package rows changed."""
    if package_plan_tuple(fresh.packages) != reviewed_plan:
        raise PackageUpdateError(
            PackageUpdateOutcome.PLAN_CHANGED,
            "the package plan changed; scan, review, and approve again",
        )


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

    async def async_plan(
        self, expected_node: str, vmid: int
    ) -> ParsedAptSimulation:
        """Return a fresh execution-time simulation without metadata refresh."""

    async def async_update(
        self, expected_node: str, vmid: int
    ) -> PackageMutationResult:
        """Run the fixed package mutation and return sane inventory evidence."""

    async def async_ping(self, expected_node: str, vmid: int) -> bool:
        """Return whether the fixed guest liveness command succeeded."""


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
        proxmox_getter: ProxmoxGetter | None = None,
        sleep: Sleeper = asyncio.sleep,
        on_retained_snapshots: RetainedSnapshotCallback = _noop_retained,
        on_update_complete: UpdateCompleteCallback = _noop_complete,
    ) -> None:
        """Initialize the package subsystem boundary."""
        self._hass = hass
        self._config_entry = config_entry
        self._transport = transport
        self._on_state_change = on_state_change
        self._now = now
        self._proxmox_getter = proxmox_getter
        self._sleep = sleep
        self._on_retained_snapshots = on_retained_snapshots
        self._on_update_complete = on_update_complete
        self._records: dict[tuple[str, int], PackageScanRecord] = {}
        self._tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._update_records: dict[tuple[str, int], PackageUpdateRecord] = {}
        self._update_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._viewed_tokens: dict[tuple[str, int], str] = {}
        self._scan_slots = asyncio.Semaphore(_MAX_GLOBAL_SCANS)
        self._update_slot = asyncio.Semaphore(1)

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

    def update_record(self, node: str, vmid: int) -> PackageUpdateRecord:
        """Return the latest ephemeral package-update state for one LXC."""
        return self._update_records.get((node, vmid), PackageUpdateRecord())

    @callback
    def async_prune(self, current_targets: Collection[tuple[str, int]]) -> None:
        """Discard scan state for VMIDs no longer present upstream.

        Deleting the record alone would not stop a stale in-flight scan for
        a removed VMID from completing later and recreating the pruned
        result; :meth:`_async_run_scan` only ever writes back to the record
        object it started with, so an attempt for a pruned (or since
        reused) target can never resurrect stale evidence.
        """
        stale = [
            key
            for key in self._records.keys() | self._update_records.keys()
            if key not in current_targets
        ]
        if not stale:
            return
        for key in stale:
            self._records.pop(key, None)
            self._update_records.pop(key, None)
            self._viewed_tokens.pop(key, None)
            task = self._tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
            update_task = self._update_tasks.pop(key, None)
            if update_task is not None and not update_task.done():
                update_task.cancel()
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
        if any(
            target_vmid == vmid and record.status is PackageUpdateStatus.RUNNING
            for (_node, target_vmid), record in self._update_records.items()
        ):
            raise PackageScanError(
                PackageScanFailure.PACKAGE_MANAGER_BUSY,
                "a package update is already running for this LXC VMID",
            )

        attempted_at = self._now()
        self._viewed_tokens.pop((node, vmid), None)
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
        self._viewed_tokens.pop((node, vmid), None)
        return True

    @callback
    def mark_viewed(self, node: str, vmid: int, token: str) -> bool:
        """Remember only the token for the exact successful plan just rendered."""
        record = self._records.get((node, vmid))
        if (
            record is None
            or record.status is not PackageScanStatus.SUCCESS
            or record.result is None
            or not record.result.packages
            or record.token != token
        ):
            return False
        self._viewed_tokens[(node, vmid)] = token
        self._on_state_change()
        return True

    def viewed_token(self, node: str, vmid: int) -> str | None:
        """Return the ephemeral token stored by the Review button."""
        return self._viewed_tokens.get((node, vmid))

    @callback
    def confirm_viewed_review(self, node: str, vmid: int) -> bool:
        """Approve exactly the viewed token through existing confirmation logic."""
        token = self._viewed_tokens.get((node, vmid))
        return token is not None and self.confirm_review(node, vmid, token)

    @callback
    def async_start_update(
        self,
        node: str,
        vmid: int,
        *,
        target_is_running: bool,
        snapshot_permission: bool,
    ) -> asyncio.Task[None]:
        """Accept a reviewed plan, invalidate scan evidence, and start update."""
        if not self.configured or self._proxmox_getter is None:
            raise PackageUpdateError(
                PackageUpdateOutcome.HELPER_OUTDATED,
                "package update prerequisites are not configured",
            )
        if not target_is_running:
            raise PackageUpdateError(
                PackageUpdateOutcome.GUEST_UNAVAILABLE,
                "LXC is not present and running in current Proxmox data",
            )
        if not snapshot_permission:
            raise PackageUpdateError(
                PackageUpdateOutcome.SNAPSHOT_FAILED,
                "VM.Snapshot permission is required for package updates",
            )
        if any(
            target_vmid == vmid and record.status is PackageScanStatus.RUNNING
            for (_node, target_vmid), record in self._records.items()
        ) or any(
            target_vmid == vmid and record.status is PackageUpdateStatus.RUNNING
            for (_node, target_vmid), record in self._update_records.items()
        ):
            raise PackageUpdateError(
                PackageUpdateOutcome.PACKAGE_MANAGER_BUSY,
                "a package scan or update is already running for this LXC VMID",
            )

        reviewed = self._records.get((node, vmid))
        if (
            reviewed is None
            or reviewed.status is not PackageScanStatus.SUCCESS
            or reviewed.result is None
            or not reviewed.result.packages
            or not reviewed.reviewed
        ):
            raise PackageUpdateError(
                PackageUpdateOutcome.PLAN_FAILED,
                "a current non-empty reviewed package plan is required",
            )

        attempted_at = self._now()
        reviewed_plan = package_plan_tuple(reviewed.result.packages)
        # Old package evidence is invalid from the instant Update is accepted.
        self._viewed_tokens.pop((node, vmid), None)
        self._set_record(node, vmid, PackageScanRecord())
        own_record = PackageUpdateRecord(
            status=PackageUpdateStatus.RUNNING,
            last_attempt=attempted_at,
        )
        self._set_update_record(node, vmid, own_record)
        task = self._config_entry.async_create_background_task(
            self._hass,
            self._async_run_update(
                node, vmid, attempted_at, reviewed_plan, own_record
            ),
            f"package update {node}/{vmid}",
        )
        self._update_tasks[(node, vmid)] = task
        return task

    async def _async_run_update(
        self,
        node: str,
        vmid: int,
        attempted_at: datetime,
        reviewed_plan: tuple[tuple[str, str, str, str], ...],
        own_record: PackageUpdateRecord,
    ) -> None:
        """Run the accepted plan/snapshot/mutation/sanity/liveness lifecycle."""
        snapshot_name: str | None = None
        snapshot_attempted = False
        snapshot_ready = False
        mutation_started = False
        changed_count: int | None = None
        try:
            async with self._update_slot:
                fresh = await self._transport.async_plan(node, vmid)
                _require_plan_unchanged(fresh, reviewed_plan)

                proxmox = self._proxmox_getter()
                rows = await async_list_snapshots(
                    proxmox,
                    node,
                    vmid,
                    executor=self._hass.async_add_executor_job,
                )
                retained = retained_snapshot_names(rows)
                if retained:
                    self._notify_retained_snapshots(node, vmid, retained)

                snapshot_name = generate_snapshot_name()
                snapshot_attempted = True
                await async_create_snapshot(
                    proxmox,
                    node,
                    vmid,
                    snapshot_name,
                    executor=self._hass.async_add_executor_job,
                    sleep=self._sleep,
                )
                snapshot_ready = True

                mutation_started = True
                mutation = await self._transport.async_update(node, vmid)
                changed_count = changed_package_count(mutation)

                await self._async_require_liveness(proxmox, node, vmid)

                try:
                    await async_delete_snapshot(
                        proxmox,
                        node,
                        vmid,
                        snapshot_name,
                        executor=self._hass.async_add_executor_job,
                        sleep=self._sleep,
                    )
                except SnapshotError:
                    outcome = PackageUpdateRecord(
                        status=PackageUpdateStatus.SUCCESS,
                        last_attempt=attempted_at,
                        outcome=PackageUpdateOutcome.SUCCESS,
                        snapshot_retained=True,
                        snapshot_cleanup_failed=True,
                        snapshot_name=snapshot_name,
                        liveness=True,
                        changed_package_count=changed_count,
                        error_message="package update succeeded but snapshot cleanup failed",
                    )
                else:
                    outcome = PackageUpdateRecord(
                        status=PackageUpdateStatus.SUCCESS,
                        last_attempt=attempted_at,
                        outcome=PackageUpdateOutcome.SUCCESS,
                        liveness=True,
                        changed_package_count=changed_count,
                    )
        except PackageUpdateError as err:
            outcome = PackageUpdateRecord(
                status=PackageUpdateStatus.FAILED,
                last_attempt=attempted_at,
                outcome=err.outcome,
                snapshot_retained=snapshot_ready,
                snapshot_name=snapshot_name if snapshot_ready else None,
                liveness=False
                if err.outcome
                in {
                    PackageUpdateOutcome.GUEST_UNAVAILABLE,
                    PackageUpdateOutcome.LIVENESS_FAILED,
                }
                else None,
                changed_package_count=changed_count,
                error_message=str(err)[:_MAX_ERROR_MESSAGE_LENGTH],
            )
        except SnapshotError as err:
            outcome = PackageUpdateRecord(
                status=PackageUpdateStatus.FAILED,
                last_attempt=attempted_at,
                outcome=PackageUpdateOutcome.SNAPSHOT_FAILED,
                snapshot_retained=snapshot_attempted and err.may_exist,
                snapshot_name=(
                    snapshot_name if snapshot_attempted and err.may_exist else None
                ),
                error_message=str(err)[:_MAX_ERROR_MESSAGE_LENGTH],
            )
        except asyncio.CancelledError:
            outcome = PackageUpdateRecord(
                status=PackageUpdateStatus.FAILED,
                last_attempt=attempted_at,
                outcome=(
                    PackageUpdateOutcome.MUTATION_UNCERTAIN
                    if mutation_started
                    else PackageUpdateOutcome.SNAPSHOT_FAILED
                ),
                snapshot_retained=snapshot_attempted,
                snapshot_name=snapshot_name if snapshot_attempted else None,
                changed_package_count=changed_count,
                error_message="package update was interrupted",
            )
            if self._update_records.get((node, vmid)) is own_record:
                self._finish_update(node, vmid, own_record, outcome)
            else:
                # Pruning removes target state before cancelling its task. The
                # record must stay pruned, but a possibly retained safety
                # snapshot still requires an operator-visible result.
                self._notify_update_complete(node, vmid, outcome)
            raise
        except Exception:
            _LOGGER.exception("Unexpected package update failure for %s/%s", node, vmid)
            outcome = PackageUpdateRecord(
                status=PackageUpdateStatus.FAILED,
                last_attempt=attempted_at,
                outcome=(
                    PackageUpdateOutcome.MUTATION_UNCERTAIN
                    if mutation_started
                    else PackageUpdateOutcome.SNAPSHOT_FAILED
                ),
                snapshot_retained=snapshot_attempted,
                snapshot_name=snapshot_name if snapshot_attempted else None,
                changed_package_count=changed_count,
                error_message="unexpected package update failure",
            )
        self._finish_update(node, vmid, own_record, outcome)

    async def _async_require_liveness(self, proxmox: Any, node: str, vmid: int) -> None:
        """Require native running state plus one bounded fixed-command PONG."""
        try:
            status = await self._hass.async_add_executor_job(
                lambda: proxmox.nodes(node).lxc(vmid).status.current.get()
            )
        except Exception as err:
            raise PackageUpdateError(
                PackageUpdateOutcome.GUEST_UNAVAILABLE,
                "could not confirm native PVE LXC running state",
            ) from err
        if not isinstance(status, Mapping) or status.get("status") != "running":
            raise PackageUpdateError(
                PackageUpdateOutcome.GUEST_UNAVAILABLE,
                "native PVE reports the LXC is not running",
            )
        for attempt in range(2):
            try:
                if await self._transport.async_ping(node, vmid):
                    return
            except PackageUpdateError:
                pass
            if attempt == 0:
                await self._sleep(_LIVENESS_RETRY_SECONDS)
        raise PackageUpdateError(
            PackageUpdateOutcome.LIVENESS_FAILED,
            "LXC did not answer the package-update liveness probe",
        )

    @callback
    def _finish_update(
        self,
        node: str,
        vmid: int,
        own_record: PackageUpdateRecord,
        outcome: PackageUpdateRecord,
    ) -> None:
        """Publish an update outcome only while this attempt still owns the target."""
        if self._update_records.get((node, vmid)) is not own_record:
            return
        self._update_tasks.pop((node, vmid), None)
        self._set_update_record(node, vmid, outcome)
        self._notify_update_complete(node, vmid, outcome)

    def _notify_update_complete(
        self, node: str, vmid: int, outcome: PackageUpdateRecord
    ) -> None:
        """Publish one bounded terminal result without affecting its truth."""
        try:
            self._on_update_complete(node, vmid, outcome)
        except Exception:
            _LOGGER.exception(
                "Could not publish package update result for %s/%s", node, vmid
            )

    def _notify_retained_snapshots(
        self, node: str, vmid: int, names: tuple[str, ...]
    ) -> None:
        """Publish a warning without allowing presentation to fail the update."""
        try:
            self._on_retained_snapshots(node, vmid, names)
        except Exception:
            _LOGGER.exception(
                "Could not publish retained snapshot warning for %s/%s", node, vmid
            )

    @callback
    def _set_record(self, node: str, vmid: int, record: PackageScanRecord) -> None:
        """Atomically replace one record and notify summary entities."""
        self._records[(node, vmid)] = record
        self._on_state_change()

    @callback
    def _set_update_record(
        self, node: str, vmid: int, record: PackageUpdateRecord
    ) -> None:
        """Atomically replace one update record and notify summary entities."""
        self._update_records[(node, vmid)] = record
        self._on_state_change()
