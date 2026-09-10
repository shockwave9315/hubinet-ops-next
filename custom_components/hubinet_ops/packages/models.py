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
