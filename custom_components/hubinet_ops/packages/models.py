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


@dataclass(slots=True)
class PackageScanError(Exception):
    """A bounded package scan failure safe to expose to Home Assistant."""

    failure: PackageScanFailure
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
class PackageScanResult:
    """Complete package scan result for one LXC guest."""

    os_id: str
    os_version: str
    packages: tuple[PendingPackage, ...]
    reboot_required: bool | None
    not_upgraded_count: int


@dataclass(frozen=True, slots=True)
class PackageScanRecord:
    """Ephemeral state for the latest scan attempt of one LXC guest."""

    status: PackageScanStatus = PackageScanStatus.NEVER
    last_attempt: datetime | None = None
    result: PackageScanResult | None = None
    failure: PackageScanFailure | None = None
    error_message: str | None = None
