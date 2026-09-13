"""Typed package-scan state and evidence."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PackageScanFailure(StrEnum):
    """Bounded package scan failure classes."""

    DPKG_UNFINISHED = "dpkg_unfinished"
    EXECUTION_FAILED = "execution_failed"
    GUEST_CHANGED_DURING_SCAN = "guest_changed_during_scan"
    GUEST_UNAVAILABLE = "guest_unavailable"
    IDENTITY_MISMATCH = "identity_mismatch"
    MALFORMED_PLAN = "malformed_plan"
    METADATA_REFRESH_FAILED = "metadata_refresh_failed"
    PACKAGE_MANAGER_BUSY = "package_manager_busy"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    SIMULATION_FAILED = "simulation_failed"
    TIMEOUT = "timeout"
    UNSUPPORTED_OS = "unsupported_os"


class PackageScanStatus(StrEnum):
    """Latest package-scan attempt state."""

    NEVER = "never"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class PackageUpdateOutcome(StrEnum):
    """Bounded outcome for a package update attempt."""

    PLAN_CHANGED = "plan_changed"
    PLAN_FAILED = "plan_failed"
    SNAPSHOT_FAILED = "snapshot_failed"
    PACKAGE_MANAGER_BUSY = "package_manager_busy"
    MUTATION_FAILED = "mutation_failed"
    MUTATION_TIMED_OUT = "mutation_timed_out"
    MUTATION_UNCERTAIN = "mutation_uncertain"
    GUEST_UNAVAILABLE = "guest_unavailable"
    LIVENESS_FAILED = "liveness_failed"
    HELPER_OUTDATED = "helper_outdated"
    SUCCESS = "success"


class PackageUpdateStatus(StrEnum):
    """Latest in-memory package update attempt state."""

    NEVER = "never"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class HealthCheckStatus(StrEnum):
    """Lifecycle status of the latest ephemeral Health check attempt."""

    NEVER = "never"
    RUNNING = "running"
    COMPLETED = "completed"


class HealthState(StrEnum):
    """Positive Health verdicts. UNKNOWN is native ``None``, not a member."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class HealthSource(StrEnum):
    """What triggered the latest Health check attempt."""

    MANUAL = "manual"
    UPDATE = "update"
    AUTOREMOVE = "autoremove"


class HealthDpkgState(StrEnum):
    """Bounded dpkg classification returned by the ``check_health`` helper op."""

    OK = "ok"
    INTERRUPTED = "interrupted"
    PENDING = "pending"
    BUSY = "busy"
    LOCK_UNKNOWN = "lock_unknown"
    CHANGED = "changed"


class HealthReason(StrEnum):
    """Bounded reasons a Health check did not reach a positive verdict."""

    GUEST_UNAVAILABLE = "guest_unavailable"
    GUEST_EXEC_FAILED = "guest_exec_failed"
    TIMEOUT = "timeout"
    HELPER_OUTDATED = "helper_outdated"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    TRANSPORT_FAILED = "transport_failed"
    MALFORMED_EVIDENCE = "malformed_evidence"
    UNSUPPORTED_GUEST = "unsupported_guest"
    PACKAGE_MANAGER_BUSY = "package_manager_busy"
    DPKG_PENDING = "dpkg_pending"
    DPKG_INTERRUPTED = "dpkg_interrupted"
    DPKG_LOCK_UNKNOWN = "dpkg_lock_unknown"
    GUEST_CHANGED = "guest_changed"


@dataclass(slots=True)
class PackageScanError(Exception):
    """A bounded package scan failure safe to expose to Home Assistant."""

    failure: PackageScanFailure
    message: str

    def __str__(self) -> str:
        """Return the bounded failure message."""
        return self.message


@dataclass(slots=True)
class PackageUpdateError(Exception):
    """A bounded package update failure safe to expose to Home Assistant."""

    outcome: PackageUpdateOutcome
    message: str

    def __str__(self) -> str:
        """Return the bounded failure message."""
        return self.message


@dataclass(slots=True)
class PackageHealthError(Exception):
    """A Health check outcome that stopped short of usable evidence."""

    reason: HealthReason
    message: str

    def __str__(self) -> str:
        """Return the bounded failure message."""
        return self.message


@dataclass(frozen=True, slots=True)
class PendingPackage:
    """One installed binary package with a newer candidate version."""

    name: str
    architecture: str
    installed_version: str
    candidate_version: str
    origin: str | None
    security: bool | None


@dataclass(frozen=True, slots=True)
class RemovablePackage:
    """One exact installed package APT considers safe to autoremove."""

    name: str
    architecture: str
    installed_version: str


@dataclass(frozen=True, slots=True)
class CleanupEvidence:
    """One successful ephemeral observation of the exact cleanup plan."""

    candidates: tuple[RemovablePackage, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PackageScanResult:
    """Complete package scan result for one LXC guest."""

    os_id: str
    os_version: str
    packages: tuple[PendingPackage, ...]
    reboot_required: bool | None
    not_upgraded_count: int


@dataclass(frozen=True, slots=True)
class PackageMutationResult:
    """Parsed package inventories observed immediately around mutation."""

    before: dict[tuple[str, str], str]
    after: dict[tuple[str, str], str]


@dataclass(frozen=True, slots=True)
class PackageUpdateRecord:
    """Small ephemeral outcome of the latest bounded package mutation attempt."""

    status: PackageUpdateStatus = PackageUpdateStatus.NEVER
    last_attempt: datetime | None = None
    outcome: PackageUpdateOutcome | None = None
    snapshot_retained: bool = False
    snapshot_uncertain: bool = False
    snapshot_cleanup_failed: bool = False
    snapshot_name: str | None = None
    liveness: bool | None = None
    changed_package_count: int | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class PackageScanRecord:
    """Ephemeral state for the latest scan attempt of one LXC guest.

    ``token`` and ``reviewed`` carry package-review state on this same
    record; there is no separate review object, copy, or history. ``token``
    is present only for a successful attempt and is a fresh opaque value
    for optimistic concurrency between viewing and confirming a plan -- it
    is not a credential, identity, or persisted value. ``reviewed`` is true
    only once this exact record's plan has been explicitly confirmed.
    """

    status: PackageScanStatus = PackageScanStatus.NEVER
    last_attempt: datetime | None = None
    result: PackageScanResult | None = None
    failure: PackageScanFailure | None = None
    error_message: str | None = None
    token: str | None = None
    reviewed: bool = False


@dataclass(frozen=True, slots=True)
class PackageHealthEvidence:
    """Raw bounded per-guest OS/package Health evidence from the helper.

    Only bounded enums/bools/ints -- never guest-controlled text, package
    names, or command output.
    """

    guest_exec: bool
    guest_exec_unavailable: bool
    dpkg: HealthDpkgState | None
    unfinished_package_count: int | None
    reboot_required: bool | None


@dataclass(frozen=True, slots=True)
class PackageHealthOutcome:
    """One classified Health verdict plus the raw evidence behind it."""

    state: HealthState | None
    reason: HealthReason | None
    evidence: PackageHealthEvidence | None


@dataclass(frozen=True, slots=True)
class PackageHealthRecord:
    """Small ephemeral outcome of the latest Health check attempt for one LXC."""

    check_status: HealthCheckStatus = HealthCheckStatus.NEVER
    state: HealthState | None = None
    reason: HealthReason | None = None
    source: HealthSource | None = None
    checked_at: datetime | None = None
    guest_exec: bool | None = None
    dpkg: HealthDpkgState | None = None
    unfinished_package_count: int | None = None
    reboot_required: bool | None = None


def classify_health(evidence: PackageHealthEvidence) -> PackageHealthOutcome:
    """Classify raw Health evidence into one verdict, per the accepted rules.

    FAILED requires positive evidence only. Any other unresolved condition
    is UNKNOWN (``state is None``), and UNKNOWN from a required check
    prevents both HEALTHY and DEGRADED. FAILED takes precedence over
    DEGRADED.
    """
    if not evidence.guest_exec:
        if evidence.guest_exec_unavailable:
            return PackageHealthOutcome(
                None, HealthReason.GUEST_UNAVAILABLE, evidence
            )
        return PackageHealthOutcome(
            HealthState.FAILED, HealthReason.GUEST_EXEC_FAILED, evidence
        )

    dpkg_to_outcome: dict[HealthDpkgState, tuple[HealthState | None, HealthReason | None]] = {
        HealthDpkgState.INTERRUPTED: (HealthState.FAILED, HealthReason.DPKG_INTERRUPTED),
        HealthDpkgState.PENDING: (None, HealthReason.DPKG_PENDING),
        HealthDpkgState.BUSY: (None, HealthReason.PACKAGE_MANAGER_BUSY),
        HealthDpkgState.LOCK_UNKNOWN: (None, HealthReason.DPKG_LOCK_UNKNOWN),
        HealthDpkgState.CHANGED: (None, HealthReason.GUEST_CHANGED),
    }
    if evidence.dpkg is None:
        return PackageHealthOutcome(None, HealthReason.MALFORMED_EVIDENCE, evidence)
    if evidence.dpkg is HealthDpkgState.OK:
        if evidence.reboot_required:
            return PackageHealthOutcome(HealthState.DEGRADED, None, evidence)
        return PackageHealthOutcome(HealthState.HEALTHY, None, evidence)
    state, reason = dpkg_to_outcome[evidence.dpkg]
    return PackageHealthOutcome(state, reason, evidence)
