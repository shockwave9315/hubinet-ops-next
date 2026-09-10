"""Strict parsers for Debian and Ubuntu package scan evidence."""

from collections.abc import Mapping
from dataclasses import dataclass
import re
import shlex

from .models import (
    PackageScanError,
    PackageScanFailure,
    PendingPackage,
    RemovablePackage,
)

_INST_RE = re.compile(
    r"^Inst (?P<name>\S+) \[(?P<installed>[^\]\s]+)\] "
    r"\((?P<candidate>\S+) (?P<relstr>[^)]*)\)"
    r"(?: \[[^\[\]\r\n]*\])*$"
)
_CONF_RE = re.compile(
    r"^Conf (?P<name>\S+) \((?P<candidate>\S+) (?P<relstr>[^)]*)\)"
    r"(?: \[[^\[\]\r\n]*\])*$"
)
_REMV_RE = re.compile(
    r"^Remv (?P<name>\S+) \[(?P<installed>[^\]\s]+)\]"
    r"(?: \[[^\[\]\r\n]*\])*$"
)
_RELSTR_RE = re.compile(r"(?:(?P<origin>.*?) )?\[(?P<architecture>[^\[\]]*)\]")
_ARCHITECTURE_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_SUMMARY_RE = re.compile(
    r"^(?P<upgraded>\d{1,9}) upgraded, (?P<new>\d{1,9}) newly installed, "
    r"(?P<removed>\d{1,9}) to remove and (?P<held>\d{1,9}) not upgraded\.$"
)
_BAD_COUNT_RE = re.compile(r"^\d{1,9} not fully installed or removed\.$")
_SECURITY_ORIGIN_RE = re.compile(
    r"(?:^|[/ :])[^ /:]*-security(?:$|[/ :])", re.IGNORECASE
)

DPKG_STATUS_WORDS = frozenset(
    {
        "installed",
        "not-installed",
        "config-files",
        "half-installed",
        "unpacked",
        "half-configured",
        "triggers-awaited",
        "triggers-pending",
    }
)
DPKG_UNFINISHED_STATUS_WORDS = frozenset(
    {
        "half-installed",
        "unpacked",
        "half-configured",
        "triggers-awaited",
        "triggers-pending",
    }
)


class PackageScanParseError(ValueError):
    """Package scan evidence was malformed or ambiguous."""

    def __init__(
        self,
        message: str,
        failure: PackageScanFailure = PackageScanFailure.MALFORMED_PLAN,
    ) -> None:
        """Initialize an evidence failure with a semantic classification."""
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class InstalledInventory:
    """Installed packages and any unfinished dpkg state."""

    installed: dict[tuple[str, str], str]
    unfinished: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class ParsedAptSimulation:
    """Exact pending rows plus APT's separate kept-back count."""

    packages: tuple[PendingPackage, ...]
    not_upgraded_count: int


@dataclass(frozen=True, slots=True)
class ParsedAutoremoveSimulation:
    """Exact package identities from one cleanup-only APT simulation."""

    packages: tuple[RemovablePackage, ...]
    not_upgraded_count: int


def parse_os_release(text: str) -> tuple[str, str]:
    """Parse and validate bounded Debian or Ubuntu os-release evidence."""
    if not isinstance(text, str) or len(text.encode()) > 64 * 1024:
        raise PackageScanParseError("OS release evidence is missing or oversized")
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PackageScanParseError("OS release evidence contains a malformed line")
        key, raw_value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise PackageScanParseError("OS release evidence contains an invalid field")
        try:
            parsed = shlex.split(raw_value, posix=True)
        except ValueError as err:
            raise PackageScanParseError(
                "OS release evidence contains invalid quoting"
            ) from err
        if len(parsed) > 1:
            raise PackageScanParseError(
                "OS release evidence contains an ambiguous value"
            )
        values[key] = parsed[0] if parsed else ""
    os_id = values.get("ID", "").lower()
    version = values.get("VERSION_ID") or values.get("VERSION_CODENAME") or ""
    if os_id not in {"debian", "ubuntu"}:
        raise PackageScanError(
            PackageScanFailure.UNSUPPORTED_OS,
            "guest operating system is not Debian or Ubuntu",
        )
    if not version or len(version) > 200:
        raise PackageScanParseError("OS release evidence has no bounded version")
    return os_id, version


def parse_native_architecture(text: str) -> str:
    """Parse the guest's dpkg native architecture."""
    if not isinstance(text, str) or len(text.encode()) > 4096:
        raise PackageScanParseError(
            "native architecture evidence is missing or oversized"
        )
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise PackageScanParseError("native architecture evidence is ambiguous")
    architecture = lines[0].strip()
    if architecture == "all" or not _ARCHITECTURE_RE.fullmatch(architecture):
        raise PackageScanParseError("native architecture evidence is malformed")
    return architecture


def parse_installed_inventory(text: str) -> InstalledInventory:
    """Parse independently observed dpkg package state."""
    if not isinstance(text, str) or len(text.encode()) > 16 * 1024 * 1024:
        raise PackageScanParseError(
            "installed package inventory evidence is missing or oversized"
        )
    installed: dict[tuple[str, str], str] = {}
    unfinished: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split("\t")
        if len(fields) != 4:
            raise PackageScanParseError(
                "installed package inventory contains a malformed row"
            )
        name, architecture, version, status = fields
        if status not in DPKG_STATUS_WORDS:
            raise PackageScanParseError(
                "installed package inventory contains an unknown status"
            )
        if status == "not-installed":
            continue
        if (
            not name
            or len(name) > 300
            or len(version) > 500
            or not _ARCHITECTURE_RE.fullmatch(architecture)
        ):
            raise PackageScanParseError(
                "installed package inventory contains a malformed row"
            )
        identity = (name, architecture)
        if identity in seen:
            raise PackageScanParseError(
                "installed package inventory contains a duplicate identity"
            )
        seen.add(identity)
        if status in DPKG_UNFINISHED_STATUS_WORDS:
            unfinished.append((name, architecture, status))
        elif status == "installed":
            if not version:
                raise PackageScanParseError(
                    "installed package inventory contains a malformed row"
                )
            installed[identity] = version
    return InstalledInventory(installed, tuple(sorted(unfinished)))


def _split_qualified_name(raw_name: str) -> tuple[str, str | None]:
    if ":" not in raw_name:
        return raw_name, None
    name, _, architecture = raw_name.partition(":")
    return name, architecture


def _parse_candidate_description(relstr: str) -> tuple[str | None, str]:
    match = _RELSTR_RE.fullmatch(relstr)
    if match is None:
        raise PackageScanParseError(
            "APT simulation candidate description has no architecture"
        )
    architecture = match.group("architecture")
    if not _ARCHITECTURE_RE.fullmatch(architecture or ""):
        raise PackageScanParseError("APT simulation architecture is malformed")
    origin = (match.group("origin") or "").strip() or None
    if origin is not None and len(origin) > 500:
        origin = None
    return origin, architecture


def _resolve_installed_architecture(
    name: str,
    qualified_architecture: str | None,
    native_architecture: str,
    inventory: Mapping[tuple[str, str], str],
) -> str:
    candidates = (
        (qualified_architecture,)
        if qualified_architecture is not None
        else (native_architecture, "all")
    )
    matches = [
        architecture for architecture in candidates if (name, architecture) in inventory
    ]
    if len(matches) != 1:
        raise PackageScanParseError(
            "APT package no longer has an unambiguous installed identity",
            PackageScanFailure.GUEST_CHANGED_DURING_SCAN,
        )
    return matches[0]


def parse_apt_simulation(  # noqa: C901
    text: str, *, native_architecture: str, installed_inventory: str
) -> ParsedAptSimulation:
    """Parse an exact upgrade-only APT simulation plan."""
    if not isinstance(text, str) or len(text.encode()) > 8 * 1024 * 1024:
        raise PackageScanParseError("APT simulation output is missing or oversized")
    native = parse_native_architecture(native_architecture)
    inventory_state = parse_installed_inventory(installed_inventory)
    if inventory_state.unfinished:
        raise PackageScanParseError(
            "dpkg reports unfinished package state",
            PackageScanFailure.DPKG_UNFINISHED,
        )

    changes: list[tuple[str, str, str | None, str, str, str, str | None]] = []
    seen_inst: set[str] = set()
    seen_conf: set[str] = set()
    pending_conf: list[tuple[str, str, str]] = []
    summary: tuple[int, int, int, int] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("Remv ", "Purg ")):
            raise PackageScanParseError("APT simulation unexpectedly planned a removal")
        if line.startswith("Inst "):
            match = _INST_RE.fullmatch(line)
            if match is None:
                raise PackageScanParseError(
                    "APT simulation contains an unparseable change"
                )
            raw_name = match.group("name")
            if raw_name in seen_inst:
                raise PackageScanParseError(
                    "APT simulation contains a duplicate change"
                )
            seen_inst.add(raw_name)
            name, qualified_architecture = _split_qualified_name(raw_name)
            origin, candidate_architecture = _parse_candidate_description(
                match.group("relstr")
            )
            if (
                qualified_architecture is not None
                and qualified_architecture != candidate_architecture
            ):
                raise PackageScanParseError(
                    "APT package name architecture contradicts its candidate"
                )
            changes.append(
                (
                    raw_name,
                    name,
                    qualified_architecture,
                    candidate_architecture,
                    match.group("installed"),
                    match.group("candidate"),
                    origin,
                )
            )
            continue
        if line.startswith("Conf "):
            match = _CONF_RE.fullmatch(line)
            if match is None:
                raise PackageScanParseError(
                    "APT simulation contains an unparseable configure action"
                )
            raw_name = match.group("name")
            _, architecture = _parse_candidate_description(match.group("relstr"))
            _, qualified_architecture = _split_qualified_name(raw_name)
            if (
                qualified_architecture is not None
                and qualified_architecture != architecture
            ):
                raise PackageScanParseError(
                    "APT configure architecture is contradictory"
                )
            if raw_name in seen_conf:
                raise PackageScanParseError(
                    "APT simulation contains a duplicate configure action"
                )
            seen_conf.add(raw_name)
            pending_conf.append((raw_name, match.group("candidate"), architecture))
            continue
        if _BAD_COUNT_RE.fullmatch(line):
            raise PackageScanParseError(
                "APT reports unfinished dpkg state",
                PackageScanFailure.DPKG_UNFINISHED,
            )
        if match := _SUMMARY_RE.fullmatch(line):
            if summary is not None:
                raise PackageScanParseError(
                    "APT simulation contains duplicate summaries"
                )
            summary = (
                int(match.group("upgraded")),
                int(match.group("new")),
                int(match.group("removed")),
                int(match.group("held")),
            )

    if summary is None:
        raise PackageScanParseError("APT simulation has no exact plan summary")
    upgraded, newly_installed, removed, not_upgraded = summary
    if newly_installed or removed:
        raise PackageScanParseError("APT simulation is not an upgrade-only plan")
    if upgraded != len(changes):
        raise PackageScanParseError("APT simulation summary does not match its changes")

    packages: list[PendingPackage] = []
    identities: set[tuple[str, str]] = set()
    approved: dict[tuple[str, str], str] = {}
    for (
        raw_name,
        name,
        qualified_architecture,
        candidate_architecture,
        installed_version,
        candidate_version,
        origin,
    ) in changes:
        architecture = _resolve_installed_architecture(
            name,
            qualified_architecture,
            native,
            inventory_state.installed,
        )
        if inventory_state.installed[(name, architecture)] != installed_version:
            raise PackageScanParseError(
                "APT installed version contradicts the later dpkg inventory",
                PackageScanFailure.GUEST_CHANGED_DURING_SCAN,
            )
        if architecture != candidate_architecture:
            raise PackageScanParseError(
                "APT candidate architecture contradicts the later dpkg inventory",
                PackageScanFailure.GUEST_CHANGED_DURING_SCAN,
            )
        identity = (name, architecture)
        if identity in identities:
            raise PackageScanParseError("APT simulation contains a duplicate identity")
        identities.add(identity)
        approved[(raw_name, candidate_version)] = architecture
        packages.append(
            PendingPackage(
                name=name,
                architecture=architecture,
                installed_version=installed_version,
                candidate_version=candidate_version,
                origin=origin,
                # Positive security-origin evidence is reliable; an origin
                # that merely lacks a "-security" marker is not reliable
                # non-security evidence, so it stays unknown rather than
                # False. See ARCHITECTURE.md's security tri-state rules.
                security=(
                    True
                    if origin is not None and _SECURITY_ORIGIN_RE.search(origin)
                    else None
                ),
            )
        )

    for raw_name, candidate_version, architecture in pending_conf:
        if approved.get((raw_name, candidate_version)) != architecture:
            raise PackageScanParseError(
                "APT simulation configures a package outside the exact plan"
            )

    return ParsedAptSimulation(
        packages=tuple(
            sorted(packages, key=lambda package: (package.name, package.architecture))
        ),
        not_upgraded_count=not_upgraded,
    )


def parse_autoremove_simulation(
    text: str, *, native_architecture: str, installed_inventory: str
) -> ParsedAutoremoveSimulation:
    """Parse only APT's machine-oriented ``Remv`` simulation records."""
    if not isinstance(text, str) or len(text.encode()) > 8 * 1024 * 1024:
        raise PackageScanParseError(
            "APT autoremove simulation output is missing or oversized"
        )
    native = parse_native_architecture(native_architecture)
    inventory_state = parse_installed_inventory(installed_inventory)
    if inventory_state.unfinished:
        raise PackageScanParseError(
            "dpkg reports unfinished package state",
            PackageScanFailure.DPKG_UNFINISHED,
        )

    packages: list[RemovablePackage] = []
    identities: set[tuple[str, str]] = set()
    summary: tuple[int, int, int, int] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("Purg ", "Inst ", "Conf ")):
            raise PackageScanParseError(
                "APT autoremove simulation contains a forbidden action"
            )
        if line.startswith("Remv "):
            match = _REMV_RE.fullmatch(line)
            if match is None:
                raise PackageScanParseError(
                    "APT autoremove simulation contains an unparseable removal"
                )
            name, qualified_architecture = _split_qualified_name(match.group("name"))
            architecture = _resolve_installed_architecture(
                name,
                qualified_architecture,
                native,
                inventory_state.installed,
            )
            identity = (name, architecture)
            if identity in identities:
                raise PackageScanParseError(
                    "APT autoremove simulation contains a duplicate removal"
                )
            identities.add(identity)
            installed_version = match.group("installed")
            if inventory_state.installed[identity] != installed_version:
                raise PackageScanParseError(
                    "APT removal version contradicts the installed inventory",
                    PackageScanFailure.GUEST_CHANGED_DURING_SCAN,
                )
            packages.append(
                RemovablePackage(
                    name=name,
                    architecture=architecture,
                    installed_version=installed_version,
                )
            )
            continue
        if _BAD_COUNT_RE.fullmatch(line):
            raise PackageScanParseError(
                "APT reports unfinished dpkg state",
                PackageScanFailure.DPKG_UNFINISHED,
            )
        if match := _SUMMARY_RE.fullmatch(line):
            if summary is not None:
                raise PackageScanParseError(
                    "APT autoremove simulation contains duplicate summaries"
                )
            summary = (
                int(match.group("upgraded")),
                int(match.group("new")),
                int(match.group("removed")),
                int(match.group("held")),
            )

    if summary is None:
        raise PackageScanParseError(
            "APT autoremove simulation has no exact plan summary"
        )
    upgraded, newly_installed, removed, not_upgraded = summary
    if upgraded or newly_installed:
        raise PackageScanParseError(
            "APT autoremove simulation is not a removal-only plan"
        )
    if removed != len(packages):
        raise PackageScanParseError(
            "APT autoremove summary does not match its removals"
        )
    return ParsedAutoremoveSimulation(
        packages=tuple(
            sorted(packages, key=lambda package: (package.name, package.architecture))
        ),
        not_upgraded_count=not_upgraded,
    )
