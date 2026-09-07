"""Package scan result models."""

from dataclasses import dataclass
from enum import StrEnum


class PackageScanFailure(StrEnum):
    """Bounded package scan failure classes."""

    EXECUTION_FAILED = "execution_failed"
    GUEST_UNAVAILABLE = "guest_unavailable"
    MALFORMED_PLAN = "malformed_plan"
    METADATA_REFRESH_FAILED = "metadata_refresh_failed"
    PACKAGE_MANAGER_BUSY = "package_manager_busy"
    SIMULATION_FAILED = "simulation_failed"
    TIMEOUT = "timeout"
    UNSUPPORTED_OS = "unsupported_os"


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
    security: bool


@dataclass(frozen=True, slots=True)
class PackageScanResult:
    """Complete package scan result for one LXC guest."""

    os_id: str
    os_version: str
    packages: tuple[PendingPackage, ...]
    reboot_required: bool
