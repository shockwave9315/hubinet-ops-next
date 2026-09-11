"""Package-scan orchestration, concurrency, and ephemeral state."""

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import replace
from datetime import UTC, datetime
import logging
import secrets
from typing import Any, Protocol, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .models import (
    CleanupEvidence,
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
    RemovablePackage,
)
from .parser import ParsedAptSimulation, ParsedAutoremoveSimulation
from .snapshots import (
    RetainedSnapshotSummary,
    SnapshotError,
    async_create_snapshot,
    async_delete_snapshot,
    async_list_snapshots,
    generate_snapshot_name,
    retained_snapshot_summary,
)

_LOGGER = logging.getLogger(__name__)

_MAX_GLOBAL_SCANS = 2
_MAX_ERROR_MESSAGE_LENGTH = 500
_SCAN_TOKEN_BYTES = 16
_LIVENESS_RETRY_SECONDS = 3.0

type ProxmoxGetter = Callable[[], Any]
type Sleeper = Callable[[float], Awaitable[None]]
type RetainedSnapshotCallback = Callable[[str, int, RetainedSnapshotSummary], None]
type UpdateCompleteCallback = Callable[[str, int, PackageUpdateRecord], None]
type CleanupObservationCallback = Callable[
    [str, int, tuple[RemovablePackage, ...] | None, str], None
]
type HelperVersionCallback = Callable[[int], None]

_T = TypeVar("_T")


def _noop_retained(
    _node: str, _vmid: int, _summary: RetainedSnapshotSummary
) -> None:
    """Default retained-snapshot callback."""


def _noop_complete(_node: str, _vmid: int, _record: PackageUpdateRecord) -> None:
    """Default update-completion callback."""


def _noop_cleanup_observation(
    _node: str,
    _vmid: int,
    _candidates: tuple[RemovablePackage, ...] | None,
    _source: str,
) -> None:
    """Default cleanup-observation callback."""


def _noop_helper_version(_version: int) -> None:
    """Default helper-version callback."""


def _noop_cleanup_invalidated(_node: str, _vmid: int) -> None:
    """Default cleanup-invalidation callback."""


def _noop_review_invalidated(_node: str, _vmid: int) -> None:
    """Default review-invalidation callback."""


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


def cleanup_plan_tuple(
    packages: Collection[RemovablePackage],
) -> tuple[tuple[str, str, str], ...]:
    """Project the complete equality-bearing cleanup identity."""
    return tuple(
        (package.name, package.architecture, package.installed_version)
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


def _require_cleanup_plan_unchanged(
    fresh: ParsedAutoremoveSimulation,
    shown_plan: tuple[tuple[str, str, str], ...],
) -> None:
    """Stop before snapshot creation unless the full displayed plan is exact."""
    if cleanup_plan_tuple(fresh.packages) != shown_plan:
        raise PackageUpdateError(
            PackageUpdateOutcome.PLAN_CHANGED,
            "the cleanup plan changed; scan again",
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

    @property
    def observed_helper_version(self) -> int | None:
        """Return the latest structurally valid helper version seen."""

    async def async_prepare(self, hass: HomeAssistant) -> None:
        """Evaluate and cache local transport prerequisites once, off the loop."""

    async def async_probe(self) -> Any:
        """Return the authenticated helper probe response."""

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

    async def async_plan_autoremove(
        self, expected_node: str, vmid: int
    ) -> ParsedAutoremoveSimulation:
        """Return a fresh cleanup plan without metadata refresh."""

    async def async_autoremove(
        self, expected_node: str, vmid: int
    ) -> PackageMutationResult:
        """Run the fixed autoremove and return sane inventory evidence."""

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
        on_cleanup_complete: UpdateCompleteCallback = _noop_complete,
        on_cleanup_observation: CleanupObservationCallback = (
            _noop_cleanup_observation
        ),
        on_cleanup_invalidated: Callable[[str, int], None] = (
            _noop_cleanup_invalidated
        ),
        on_review_invalidated: Callable[[str, int], None] = (
            _noop_review_invalidated
        ),
        on_helper_version: HelperVersionCallback = _noop_helper_version,
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
        self._on_cleanup_complete = on_cleanup_complete
        self._on_cleanup_observation = on_cleanup_observation
        self._on_cleanup_invalidated = on_cleanup_invalidated
        self._on_review_invalidated = on_review_invalidated
        self._on_helper_version = on_helper_version
        self._records: dict[tuple[str, int], PackageScanRecord] = {}
        self._tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._update_records: dict[tuple[str, int], PackageUpdateRecord] = {}
        self._update_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._cleanup_evidence: dict[tuple[str, int], CleanupEvidence] = {}
        self._fenced_evidence: set[tuple[str, int]] = set()
        self._cleanup_records: dict[tuple[str, int], PackageUpdateRecord] = {}
        self._cleanup_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._helper_version: int | None = None
        self._helper_probe_task: asyncio.Task[None] | None = None
        self._viewed_tokens: dict[tuple[str, int], str] = {}
        self._scan_slots = asyncio.Semaphore(_MAX_GLOBAL_SCANS)
        self._update_slot = asyncio.Semaphore(1)

    @property
    def configured(self) -> bool:
        """Return whether package entities can perform their transport."""
        return self._transport.configured

    async def async_prepare(self) -> None:
        """Prepare locally and schedule one non-blocking helper observation."""
        await self._transport.async_prepare(self._hass)
        if self.configured and self._helper_probe_task is None:
            self._helper_probe_task = self._config_entry.async_create_background_task(
                self._hass,
                self._async_probe_helper(),
                "package helper compatibility probe",
            )

    @property
    def helper_version(self) -> int | None:
        """Return the latest truthfully observed helper version."""
        return self._helper_version

    def cleanup_evidence(self, node: str, vmid: int) -> CleanupEvidence | None:
        """Return current ephemeral cleanup evidence for one target."""
        return self._cleanup_evidence.get((node, vmid))

    def cleanup_record(self, node: str, vmid: int) -> PackageUpdateRecord:
        """Return the latest ephemeral autoremove attempt for one target."""
        return self._cleanup_records.get((node, vmid), PackageUpdateRecord())

    def record(self, node: str, vmid: int) -> PackageScanRecord:
        """Return current ephemeral scan state for one upstream identity."""
        return self._records.get((node, vmid), PackageScanRecord())

    def update_record(self, node: str, vmid: int) -> PackageUpdateRecord:
        """Return the latest ephemeral package-update state for one LXC."""
        return self._update_records.get((node, vmid), PackageUpdateRecord())

    @callback
    def actionable_presentation_targets(self) -> frozenset[tuple[str, int]]:
        """Return targets with actionable UI or work that may publish it."""
        targets = (
            set(self._viewed_tokens)
            | set(self._cleanup_evidence)
            | set(self._tasks)
            | set(self._update_tasks)
            | set(self._cleanup_tasks)
        )
        targets.update(
            key
            for key, record in self._records.items()
            if record.status is PackageScanStatus.SUCCESS
            and record.result is not None
            and record.result.packages
        )
        return frozenset(targets)

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
            for key in (
                self._records.keys()
                | self._update_records.keys()
                | self._cleanup_evidence.keys()
                | self._fenced_evidence
                | self._cleanup_records.keys()
            )
            if key not in current_targets
        ]
        if not stale:
            return
        for key in stale:
            had_scan = self._records.pop(key, None) is not None
            self._update_records.pop(key, None)
            had_cleanup = self._cleanup_evidence.pop(key, None) is not None
            self._fenced_evidence.discard(key)
            self._cleanup_records.pop(key, None)
            had_viewed = self._viewed_tokens.pop(key, None) is not None
            if had_scan or had_viewed:
                self._dismiss_review(*key)
            if had_cleanup:
                try:
                    self._on_cleanup_invalidated(*key)
                except Exception:
                    _LOGGER.exception(
                        "Could not dismiss pruned cleanup presentation for %s/%s",
                        *key,
                    )
            task = self._tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
            update_task = self._update_tasks.pop(key, None)
            if update_task is not None and not update_task.done():
                update_task.cancel()
            cleanup_task = self._cleanup_tasks.pop(key, None)
            if cleanup_task is not None and not cleanup_task.done():
                cleanup_task.cancel()
        self._on_state_change()

    @callback
    def async_invalidate_non_running(
        self, running_targets: Collection[tuple[str, int]]
    ) -> None:
        """Discard current package evidence for known targets not running."""
        stale = (
            self._records.keys()
            | self._tasks.keys()
            | self._update_tasks.keys()
            | self._cleanup_evidence.keys()
            | self._cleanup_tasks.keys()
            | self._viewed_tokens.keys()
        ) - set(running_targets)
        if not stale:
            return
        changed = False
        for key in stale:
            self._fenced_evidence.add(key)
            had_scan = self._records.pop(key, None) is not None
            had_viewed = self._viewed_tokens.pop(key, None) is not None
            had_cleanup = self._cleanup_evidence.pop(key, None) is not None
            task = self._tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
            if had_scan or had_viewed:
                self._dismiss_review(*key)
            if had_cleanup:
                try:
                    self._on_cleanup_invalidated(*key)
                except Exception:
                    _LOGGER.exception(
                        "Could not dismiss stale cleanup presentation for %s/%s",
                        *key,
                    )
            changed |= had_scan or had_viewed or had_cleanup or task is not None
        if changed:
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
        if any(
            target_vmid == vmid and record.status is PackageUpdateStatus.RUNNING
            for (_node, target_vmid), record in self._cleanup_records.items()
        ):
            raise PackageScanError(
                PackageScanFailure.PACKAGE_MANAGER_BUSY,
                "package cleanup is already running for this LXC VMID",
            )

        self._fenced_evidence.discard((node, vmid))
        attempted_at = self._now()
        self._viewed_tokens.pop((node, vmid), None)
        self._dismiss_review(node, vmid)
        self._invalidate_cleanup(node, vmid)
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
        cleanup: ParsedAutoremoveSimulation | None = None
        cleanup_failed = False
        try:
            async with self._scan_slots:
                result = await self._async_transport(
                    self._transport.async_scan(node, vmid)
                )
                try:
                    cleanup = await self._async_transport(
                        self._transport.async_plan_autoremove(node, vmid)
                    )
                except PackageUpdateError as err:
                    cleanup_failed = True
                    _LOGGER.debug(
                        "Cleanup observation after scan for %s/%s failed: %s",
                        node,
                        vmid,
                        err.outcome,
                    )
                except Exception:
                    cleanup_failed = True
                    _LOGGER.exception(
                        "Unexpected cleanup observation failure for %s/%s",
                        node,
                        vmid,
                    )
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
            if self._records.get((node, vmid)) is own_record:
                if cleanup is not None:
                    self._publish_cleanup_observation(
                        node, vmid, cleanup.packages, "scan"
                    )
                elif cleanup_failed:
                    self._publish_cleanup_observation(node, vmid, None, "scan")
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
            for records in (self._update_records, self._cleanup_records)
            for (_node, target_vmid), record in records.items()
        ):
            raise PackageUpdateError(
                PackageUpdateOutcome.PACKAGE_MANAGER_BUSY,
                "a package scan, update, or cleanup is already running for this LXC VMID",
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
        self._dismiss_review(node, vmid)
        self._invalidate_cleanup(node, vmid)
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
        snapshot_submitted = False
        snapshot_ready = False
        mutation_started = False
        changed_count: int | None = None
        try:
            async with self._update_slot:
                fresh = await self._async_transport(
                    self._transport.async_plan(node, vmid)
                )
                _require_plan_unchanged(fresh, reviewed_plan)

                proxmox = self._proxmox_getter()
                rows = await async_list_snapshots(
                    proxmox,
                    node,
                    vmid,
                    executor=self._hass.async_add_executor_job,
                )
                retained = retained_snapshot_summary(rows)
                if retained.total_count:
                    self._notify_retained_snapshots(node, vmid, retained)

                snapshot_name = generate_snapshot_name()

                def _mark_snapshot_submitted() -> None:
                    nonlocal snapshot_submitted
                    snapshot_submitted = True

                await async_create_snapshot(
                    proxmox,
                    node,
                    vmid,
                    snapshot_name,
                    executor=self._hass.async_add_executor_job,
                    sleep=self._sleep,
                    on_submit=_mark_snapshot_submitted,
                )
                snapshot_ready = True

                mutation_started = True
                mutation = await self._async_transport(
                    self._transport.async_update(node, vmid)
                )
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
                snapshot_uncertain=err.may_exist,
                snapshot_name=snapshot_name if err.may_exist else None,
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
                snapshot_retained=snapshot_ready,
                snapshot_uncertain=snapshot_submitted and not snapshot_ready,
                snapshot_name=(
                    snapshot_name if snapshot_ready or snapshot_submitted else None
                ),
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
                snapshot_retained=snapshot_ready,
                snapshot_uncertain=snapshot_submitted and not snapshot_ready,
                snapshot_name=(
                    snapshot_name if snapshot_ready or snapshot_submitted else None
                ),
                changed_package_count=changed_count,
                error_message="unexpected package update failure",
            )
        if (
            outcome.status is PackageUpdateStatus.SUCCESS
            and self._update_records.get((node, vmid)) is own_record
        ):
            try:
                cleanup = await self._async_transport(
                    self._transport.async_plan_autoremove(node, vmid)
                )
            except asyncio.CancelledError:
                if self._update_records.get((node, vmid)) is own_record:
                    self._publish_cleanup_observation(node, vmid, None, "update")
                    self._finish_update(node, vmid, own_record, outcome)
                else:
                    self._notify_update_complete(node, vmid, outcome)
                raise
            except Exception:
                _LOGGER.debug(
                    "Cleanup observation after update for %s/%s failed",
                    node,
                    vmid,
                    exc_info=True,
                )
                self._publish_cleanup_observation(node, vmid, None, "update")
            else:
                self._publish_cleanup_observation(
                    node, vmid, cleanup.packages, "update"
                )
        self._finish_update(node, vmid, own_record, outcome)

    @callback
    def async_start_autoremove(
        self,
        node: str,
        vmid: int,
        *,
        target_is_running: bool,
        snapshot_permission: bool,
    ) -> asyncio.Task[None]:
        """Consume the displayed plan and start one explicit cleanup mutation."""
        if not self.configured or self._proxmox_getter is None:
            raise PackageUpdateError(
                PackageUpdateOutcome.HELPER_OUTDATED,
                "package cleanup prerequisites are not configured",
            )
        if not target_is_running:
            raise PackageUpdateError(
                PackageUpdateOutcome.GUEST_UNAVAILABLE,
                "LXC is not present and running in current Proxmox data",
            )
        if not snapshot_permission:
            raise PackageUpdateError(
                PackageUpdateOutcome.SNAPSHOT_FAILED,
                "VM.Snapshot permission is required for package cleanup",
            )
        if any(
            target_vmid == vmid and record.status is PackageScanStatus.RUNNING
            for (_node, target_vmid), record in self._records.items()
        ) or any(
            target_vmid == vmid and record.status is PackageUpdateStatus.RUNNING
            for records in (self._update_records, self._cleanup_records)
            for (_node, target_vmid), record in records.items()
        ):
            raise PackageUpdateError(
                PackageUpdateOutcome.PACKAGE_MANAGER_BUSY,
                "a package scan, update, or cleanup is already running for this LXC VMID",
            )
        evidence = self._cleanup_evidence.get((node, vmid))
        if evidence is None or not evidence.candidates:
            raise PackageUpdateError(
                PackageUpdateOutcome.PLAN_FAILED,
                "a current displayed non-empty cleanup plan is required",
            )

        attempted_at = self._now()
        shown_plan = cleanup_plan_tuple(evidence.candidates)
        self._invalidate_cleanup(node, vmid)
        own_record = PackageUpdateRecord(
            status=PackageUpdateStatus.RUNNING,
            last_attempt=attempted_at,
        )
        self._set_cleanup_record(node, vmid, own_record)
        task = self._config_entry.async_create_background_task(
            self._hass,
            self._async_run_autoremove(
                node, vmid, attempted_at, shown_plan, own_record
            ),
            f"package cleanup {node}/{vmid}",
        )
        self._cleanup_tasks[(node, vmid)] = task
        return task

    async def _async_run_autoremove(
        self,
        node: str,
        vmid: int,
        attempted_at: datetime,
        shown_plan: tuple[tuple[str, str, str], ...],
        own_record: PackageUpdateRecord,
    ) -> None:
        """Run the existing snapshot/sanity/liveness path for explicit cleanup."""
        snapshot_name: str | None = None
        snapshot_submitted = False
        snapshot_ready = False
        mutation_started = False
        changed_count: int | None = None
        try:
            async with self._update_slot:
                fresh = await self._async_transport(
                    self._transport.async_plan_autoremove(node, vmid)
                )
                _require_cleanup_plan_unchanged(fresh, shown_plan)

                proxmox = self._proxmox_getter()
                rows = await async_list_snapshots(
                    proxmox,
                    node,
                    vmid,
                    executor=self._hass.async_add_executor_job,
                )
                retained = retained_snapshot_summary(rows)
                if retained.total_count:
                    self._notify_retained_snapshots(node, vmid, retained)

                snapshot_name = generate_snapshot_name()

                def _mark_snapshot_submitted() -> None:
                    nonlocal snapshot_submitted
                    snapshot_submitted = True

                await async_create_snapshot(
                    proxmox,
                    node,
                    vmid,
                    snapshot_name,
                    executor=self._hass.async_add_executor_job,
                    sleep=self._sleep,
                    on_submit=_mark_snapshot_submitted,
                )
                snapshot_ready = True

                mutation_started = True
                mutation = await self._async_transport(
                    self._transport.async_autoremove(node, vmid)
                )
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
                        error_message=(
                            "package cleanup succeeded but snapshot cleanup failed"
                        ),
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
                liveness=(
                    False
                    if err.outcome
                    in {
                        PackageUpdateOutcome.GUEST_UNAVAILABLE,
                        PackageUpdateOutcome.LIVENESS_FAILED,
                    }
                    else None
                ),
                changed_package_count=changed_count,
                error_message=str(err)[:_MAX_ERROR_MESSAGE_LENGTH],
            )
        except SnapshotError as err:
            outcome = PackageUpdateRecord(
                status=PackageUpdateStatus.FAILED,
                last_attempt=attempted_at,
                outcome=PackageUpdateOutcome.SNAPSHOT_FAILED,
                snapshot_uncertain=err.may_exist,
                snapshot_name=snapshot_name if err.may_exist else None,
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
                snapshot_retained=snapshot_ready,
                snapshot_uncertain=snapshot_submitted and not snapshot_ready,
                snapshot_name=(
                    snapshot_name if snapshot_ready or snapshot_submitted else None
                ),
                changed_package_count=changed_count,
                error_message="package cleanup was interrupted",
            )
            if self._cleanup_records.get((node, vmid)) is own_record:
                self._publish_cleanup_observation(node, vmid, None, "autoremove")
                self._finish_cleanup(node, vmid, own_record, outcome)
            else:
                self._notify_cleanup_complete(node, vmid, outcome)
            raise
        except Exception:
            _LOGGER.exception("Unexpected package cleanup failure for %s/%s", node, vmid)
            outcome = PackageUpdateRecord(
                status=PackageUpdateStatus.FAILED,
                last_attempt=attempted_at,
                outcome=(
                    PackageUpdateOutcome.MUTATION_UNCERTAIN
                    if mutation_started
                    else PackageUpdateOutcome.SNAPSHOT_FAILED
                ),
                snapshot_retained=snapshot_ready,
                snapshot_uncertain=snapshot_submitted and not snapshot_ready,
                snapshot_name=(
                    snapshot_name if snapshot_ready or snapshot_submitted else None
                ),
                changed_package_count=changed_count,
                error_message="unexpected package cleanup failure",
            )

        if (
            outcome.status is PackageUpdateStatus.SUCCESS
            and self._cleanup_records.get((node, vmid)) is own_record
        ):
            try:
                cleanup = await self._async_transport(
                    self._transport.async_plan_autoremove(node, vmid)
                )
            except asyncio.CancelledError:
                if self._cleanup_records.get((node, vmid)) is own_record:
                    self._publish_cleanup_observation(
                        node, vmid, None, "autoremove"
                    )
                    self._finish_cleanup(node, vmid, own_record, outcome)
                else:
                    self._notify_cleanup_complete(node, vmid, outcome)
                raise
            except Exception:
                _LOGGER.debug(
                    "Cleanup observation after autoremove for %s/%s failed",
                    node,
                    vmid,
                    exc_info=True,
                )
                self._publish_cleanup_observation(node, vmid, None, "autoremove")
            else:
                self._publish_cleanup_observation(
                    node, vmid, cleanup.packages, "autoremove"
                )
        else:
            self._publish_cleanup_observation(node, vmid, None, "autoremove")
        self._finish_cleanup(node, vmid, own_record, outcome)

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
                if await self._async_transport(
                    self._transport.async_ping(node, vmid)
                ):
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

    def _notify_cleanup_complete(
        self, node: str, vmid: int, outcome: PackageUpdateRecord
    ) -> None:
        """Publish one bounded autoremove result independently from Update."""
        try:
            self._on_cleanup_complete(node, vmid, outcome)
        except Exception:
            _LOGGER.exception(
                "Could not publish package cleanup result for %s/%s", node, vmid
            )

    def _notify_retained_snapshots(
        self, node: str, vmid: int, summary: RetainedSnapshotSummary
    ) -> None:
        """Publish a warning without allowing presentation to fail the update."""
        try:
            self._on_retained_snapshots(node, vmid, summary)
        except Exception:
            _LOGGER.exception(
                "Could not publish retained snapshot warning for %s/%s", node, vmid
            )

    async def _async_probe_helper(self) -> None:
        """Observe helper compatibility once without affecting native setup."""
        try:
            probe = await self._transport.async_probe()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Best-effort package helper probe failed", exc_info=True)
        else:
            version = getattr(probe, "helper_version", None)
            if type(version) is int and version >= 1:
                self._set_helper_version(version)
        finally:
            self._sync_helper_version()

    async def _async_transport(self, operation: Awaitable[_T]) -> _T:
        """Run one transport operation and retain any valid version observation."""
        try:
            return await operation
        finally:
            self._sync_helper_version()

    @callback
    def _sync_helper_version(self) -> None:
        """Copy only valid observed helper metadata into manager-owned state."""
        version = getattr(self._transport, "observed_helper_version", None)
        if type(version) is int and version >= 1:
            self._set_helper_version(version)

    @callback
    def _set_helper_version(self, version: int) -> None:
        """Replace the one helper scalar and update operator-facing UX."""
        if self._helper_version == version:
            return
        self._helper_version = version
        try:
            self._on_helper_version(version)
        except Exception:
            _LOGGER.exception("Could not update package helper compatibility issue")

    @callback
    def _invalidate_cleanup(self, node: str, vmid: int) -> None:
        """Make old cleanup evidence non-actionable and dismiss its presentation."""
        removed = self._cleanup_evidence.pop((node, vmid), None) is not None
        try:
            self._on_cleanup_invalidated(node, vmid)
        except Exception:
            _LOGGER.exception(
                "Could not dismiss stale cleanup presentation for %s/%s", node, vmid
            )
        if removed:
            self._on_state_change()

    def _dismiss_review(self, node: str, vmid: int) -> None:
        """Dismiss a rendered review without affecting evidence invalidation."""
        try:
            self._on_review_invalidated(node, vmid)
        except Exception:
            _LOGGER.exception(
                "Could not dismiss stale review presentation for %s/%s", node, vmid
            )

    @callback
    def _publish_cleanup_observation(
        self,
        node: str,
        vmid: int,
        candidates: tuple[RemovablePackage, ...] | None,
        source: str,
    ) -> bool:
        """Present first, then make a non-empty exact cleanup plan actionable."""
        key = (node, vmid)
        if key in self._fenced_evidence:
            candidates = None
        self._cleanup_evidence.pop(key, None)
        if candidates:
            try:
                self._on_cleanup_observation(node, vmid, candidates, source)
            except Exception:
                _LOGGER.exception(
                    "Could not present cleanup candidates for %s/%s", node, vmid
                )
                try:
                    self._on_cleanup_observation(node, vmid, None, source)
                except Exception:
                    _LOGGER.debug(
                        "Could not present fallback unknown cleanup state for %s/%s",
                        node,
                        vmid,
                        exc_info=True,
                    )
                return False
            self._cleanup_evidence[key] = CleanupEvidence(
                candidates=candidates,
                observed_at=self._now(),
            )
        elif candidates == ():
            try:
                self._on_cleanup_observation(node, vmid, candidates, source)
            except Exception:
                _LOGGER.exception(
                    "Could not present empty cleanup observation for %s/%s",
                    node,
                    vmid,
                )
            self._cleanup_evidence[key] = CleanupEvidence(
                candidates=(),
                observed_at=self._now(),
            )
        else:
            try:
                self._on_cleanup_observation(node, vmid, None, source)
            except Exception:
                _LOGGER.exception(
                    "Could not present unknown cleanup observation for %s/%s",
                    node,
                    vmid,
                )
        if candidates is not None:
            self._on_state_change()
        return candidates is not None

    @callback
    def _finish_cleanup(
        self,
        node: str,
        vmid: int,
        own_record: PackageUpdateRecord,
        outcome: PackageUpdateRecord,
    ) -> None:
        """Publish cleanup state only while this attempt owns the target."""
        if self._cleanup_records.get((node, vmid)) is not own_record:
            return
        self._cleanup_tasks.pop((node, vmid), None)
        self._set_cleanup_record(node, vmid, outcome)
        self._notify_cleanup_complete(node, vmid, outcome)

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

    @callback
    def _set_cleanup_record(
        self, node: str, vmid: int, record: PackageUpdateRecord
    ) -> None:
        """Atomically replace one cleanup mutation record."""
        self._cleanup_records[(node, vmid)] = record
        self._on_state_change()
