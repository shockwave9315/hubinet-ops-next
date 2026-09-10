#!/usr/bin/env python3
"""Root-owned forced-command PVE boundary for typed guest package operations."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
import fcntl
import json
import os
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
from typing import Any

PROTOCOL_VERSION = 1
HELPER_VERSION = 3
OPERATION_PROBE = "probe"
OPERATION_SCAN_PACKAGES = "scan_packages"
OPERATION_PLAN_PACKAGES = "plan_packages"
OPERATION_UPDATE_PACKAGES = "update_packages"
OPERATION_PING = "ping"
# Retained as a compatibility alias for existing helper tests/importers.
OPERATION = OPERATION_SCAN_PACKAGES

MAX_REQUEST_BYTES = 1024
MAX_COMMAND_OUTPUT_BYTES = 16 * 1024 * 1024
OPERATION_TIMEOUT_SECONDS = 240.0
COMMAND_TIMEOUT_SECONDS = 120.0
UPDATE_OPERATION_TIMEOUT_SECONDS = 1800.0
UPDATE_COMMAND_TIMEOUT_SECONDS = 1500.0
PING_OPERATION_TIMEOUT_SECONDS = 30.0
PING_COMMAND_TIMEOUT_SECONDS = 10.0
LOCK_DIRECTORY = "/run/lock"
PVE_LOCAL_NODE_LINK = "/etc/pve/local"
PVE_LOCAL_NODE_PREFIX = "/etc/pve/nodes/"

NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
ARCHITECTURE_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
# Only the numeric upstream feature version is gated; distribution revision
# suffixes such as "build2" or "ubuntu1" are not part of the feature check
# and are intentionally not validated here.
APT_VERSION_RE = re.compile(r"^apt (\d+)\.(\d+)\.(\d+)")
MINIMUM_APT_VERSION = (2, 1, 16)
BUSY_PATTERNS = (
    "could not get lock",
    "unable to acquire the dpkg frontend lock",
    "is another process using it",
    "could not open lock file",
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

# Scan simulation, execution-time simulation, and mutation intentionally share
# the same policy options. The only differences are simulation's ``-s`` and
# mutation's absence of it. No request material is ever appended to either
# command.
APT_HARDENED_OPTIONS = (
    "-y",
    "-o",
    "APT::Get::Upgrade-Allow-New=false",
    "-o",
    "APT::Get::Remove=false",
    "-o",
    "APT::Get::Force-Yes=false",
    "-o",
    "APT::Get::allow-downgrades=false",
    "-o",
    "APT::Get::allow-remove-essential=false",
    "-o",
    "APT::Get::allow-change-held-packages=false",
    "-o",
    "APT::Get::AllowUnauthenticated=false",
    "-o",
    "APT::Ignore-Hold=false",
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
)
APT_SIMULATION_COMMAND = (
    "env",
    "LC_ALL=C",
    "DEBIAN_FRONTEND=noninteractive",
    "apt-get",
    "-s",
    "upgrade",
    *APT_HARDENED_OPTIONS,
)
APT_MUTATION_COMMAND = (
    "env",
    "LC_ALL=C",
    "DEBIAN_FRONTEND=noninteractive",
    "apt-get",
    "upgrade",
    *APT_HARDENED_OPTIONS,
)
INVENTORY_COMMAND = (
    "env",
    "LC_ALL=C",
    "dpkg-query",
    "-W",
    "-f=${Package}\\t${Architecture}\\t${Version}\\t${db:Status-Status}\\n",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded command result."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False


# Conventional type aliases: PEP 695 `type` statements require Python 3.12,
# and the helper must at least parse and run on Python 3.11.
Runner = Callable[[tuple[str, ...], float, int], CommandResult]
Clock = Callable[[], float]
LockFactory = Callable[[int], AbstractContextManager[None]]


class RequestError(ValueError):
    """The helper request did not have its sole accepted shape."""


@dataclass(slots=True)
class ScanError(Exception):
    """A bounded package scan failure."""

    classification: str
    message: str

    def __str__(self) -> str:
        """Return the bounded message."""
        return self.message


@dataclass(frozen=True, slots=True)
class OperationDeadline:
    """One deadline shared by every command in a helper operation."""

    started: float
    timeout: float
    clock: Clock

    def remaining(self) -> float:
        """Return non-negative time remaining in the operation."""
        return max(0.0, self.timeout - (self.clock() - self.started))


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Best-effort kill of the direct child and its process group.

    ``start_new_session=True`` at spawn puts the direct child in its own
    process group, so killing that group also reaches a stray descendant
    that inherited the pipes without requiring a process supervisor.
    """
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(ProcessLookupError):
        process.kill()


def _run_bounded(
    argv: tuple[str, ...], timeout: float, max_output: int
) -> CommandResult:
    """Run one fixed argv with bounded time and combined output.

    ``start_new_session=True`` puts the direct child in its own process
    group so a stray descendant can be killed alongside it. The direct
    child exiting ends the wait for output even if a descendant still
    holds the inherited stdout/stderr pipe open; that descendant is not
    guaranteed to be reaped, matching this helper's threat model (perfect
    cancellation of guest-side processes is not required).
    """
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    started = time.monotonic()
    timed_out = output_exceeded = cleanup_failed = False

    def _combined_length() -> int:
        """Return the total bytes buffered so far, for one bound check."""
        return len(output["stdout"]) + len(output["stderr"])

    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            if process.poll() is not None:
                # The direct child already exited. Drain whatever output is
                # already buffered without blocking further; a descendant
                # that inherited the pipe must not make a successful exit
                # look like a timeout. The combined bound still applies to
                # this final drain.
                for key, _ in selector.select(timeout=0):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if chunk:
                        output[key.data].extend(chunk)
                        if _combined_length() > max_output:
                            output_exceeded = True
                            break
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                if _combined_length() > max_output:
                    output_exceeded = True
                    break
            if output_exceeded:
                break
    finally:
        selector.close()
        if timed_out or output_exceeded:
            _kill_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Cleanup itself must never raise past this function: a stuck
            # or unreapable child becomes a structured timeout instead of
            # an uncaught traceback that would bypass the JSON protocol.
            # The child may still be alive here (e.g. its pipes reached EOF
            # or the deadline passed without it having been killed above);
            # a last-resort process-group kill keeps it from continuing to
            # run on the host. One bounded retry only -- never a second
            # unguarded wait.
            cleanup_failed = True
            _kill_process_tree(process)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
    return CommandResult(
        process.returncode if process.returncode is not None else -1,
        bytes(output["stdout"][: max_output + 1]),
        bytes(output["stderr"][: max_output + 1]),
        timed_out or cleanup_failed,
        output_exceeded,
    )


@contextmanager
def _target_lock(vmid: int) -> Iterator[None]:
    """Hold a non-durable, non-blocking host lock for one LXC VMID."""
    path = f"{LOCK_DIRECTORY}/hubinet-ops-package-scan-{vmid}.lock"
    flags = os.O_CLOEXEC | os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as err:
        raise ScanError("execution_failed", "could not open package scan lock") from err
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ScanError("execution_failed", "package scan lock is not a file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise ScanError(
                "package_manager_busy", "another package scan is active for this LXC"
            ) from err
        yield
    finally:
        os.close(fd)


def validate_request(payload: Any) -> tuple[str, str | None, int | None]:
    """Validate one exact typed request shape."""
    if not isinstance(payload, Mapping):
        raise RequestError("request must be a JSON object")
    if (
        type(payload.get("protocol_version")) is not int
        or payload.get("protocol_version") != PROTOCOL_VERSION
    ):
        raise RequestError("unsupported helper protocol version")
    operation = payload.get("operation")
    if operation == OPERATION_PROBE:
        if set(payload) != {"protocol_version", "operation"}:
            raise RequestError("request must have the exact probe shape")
        return operation, None, None
    if operation not in {
        OPERATION_SCAN_PACKAGES,
        OPERATION_PLAN_PACKAGES,
        OPERATION_UPDATE_PACKAGES,
        OPERATION_PING,
    }:
        raise RequestError("unknown host-control operation")
    if set(payload) != {"protocol_version", "operation", "target"}:
        raise RequestError("request must have the exact package-operation shape")
    target = payload["target"]
    if not isinstance(target, Mapping) or set(target) != {"node", "vmid"}:
        raise RequestError("target must have the exact package-operation shape")
    node = target["node"]
    vmid = target["vmid"]
    if not isinstance(node, str) or not NODE_RE.fullmatch(node):
        raise RequestError("node must be a valid PVE node identity")
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid must be a valid PVE integer VMID")
    return operation, node, vmid


def _command(
    runner: Runner,
    deadline: OperationDeadline,
    argv: tuple[str, ...],
    *,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
    command_timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> CommandResult:
    """Run one command using no more than the global remaining time."""
    remaining = deadline.remaining()
    if remaining <= 0:
        raise ScanError("timeout", "package scan operation deadline exceeded")
    result = runner(argv, min(command_timeout, remaining), max_output)
    if result.timed_out:
        raise ScanError("timeout", "package scan command timed out")
    if deadline.remaining() <= 0:
        raise ScanError("timeout", "package scan operation deadline exceeded")
    if result.output_exceeded:
        raise ScanError("execution_failed", "package scan output exceeded its bound")
    return result


def _guest_command(
    runner: Runner,
    deadline: OperationDeadline,
    vmid: int,
    tail: tuple[str, ...],
    *,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
    command_timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> CommandResult:
    """Run one fixed command in the target LXC."""
    return _command(
        runner,
        deadline,
        ("pct", "exec", str(vmid), "--", *tail),
        max_output=max_output,
        command_timeout=command_timeout,
    )


def _decode(result: CommandResult) -> tuple[str, str]:
    """Decode bounded command output as strict UTF-8."""
    try:
        return result.stdout.decode(), result.stderr.decode()
    except UnicodeDecodeError as err:
        raise ScanError(
            "execution_failed", "package scan command output was not UTF-8"
        ) from err


def _get_local_node(runner: Runner, deadline: OperationDeadline) -> str:
    """Return the PVE-native local node identity.

    Raw ``hostname`` may be an FQDN while PVE node identity is short, so the
    local node is instead resolved from ``/etc/pve/local``, a PVE-managed
    symlink to ``/etc/pve/nodes/<local node name>``.
    """
    result = _command(
        runner, deadline, ("readlink", "-f", PVE_LOCAL_NODE_LINK), max_output=4096
    )
    stdout, _ = _decode(result)
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if (
        result.returncode != 0
        or len(lines) != 1
        or not lines[0].startswith(PVE_LOCAL_NODE_PREFIX)
    ):
        raise ScanError(
            "execution_failed", "could not establish local PVE node identity"
        )
    local_node = lines[0][len(PVE_LOCAL_NODE_PREFIX) :]
    if not NODE_RE.fullmatch(local_node):
        raise ScanError(
            "execution_failed", "local PVE node identity has an unexpected shape"
        )
    return local_node


def _validate_local_node(
    expected_node: str, runner: Runner, deadline: OperationDeadline
) -> None:
    """Verify the expected node against the PVE-native local identity."""
    local_node = _get_local_node(runner, deadline)
    if local_node != expected_node:
        raise ScanError(
            "identity_mismatch", "local PVE node does not match the expected node"
        )


def _validate_target(vmid: int, runner: Runner, deadline: OperationDeadline) -> None:
    """Check that the execution-time VMID is a running LXC."""
    config = _command(
        runner, deadline, ("pct", "config", str(vmid)), max_output=1024 * 1024
    )
    if config.returncode != 0:
        raise ScanError("guest_unavailable", "VMID is not an available LXC")
    status_result = _command(
        runner, deadline, ("pct", "status", str(vmid)), max_output=64 * 1024
    )
    status_stdout, _ = _decode(status_result)
    if status_result.returncode != 0:
        raise ScanError("guest_unavailable", "could not establish LXC runtime state")
    if status_stdout.strip() != "status: running":
        raise ScanError("guest_unavailable", "LXC is not running")


def _parse_os_release(text: str) -> tuple[str, str]:
    """Validate the guest OS before package commands are attempted."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ScanError("unsupported_os", "guest OS release metadata is malformed")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise ScanError("unsupported_os", "guest OS release metadata is malformed")
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError as err:
            raise ScanError(
                "unsupported_os", "guest OS release metadata is malformed"
            ) from err
        if len(parsed) > 1:
            raise ScanError("unsupported_os", "guest OS release metadata is ambiguous")
        values[key] = parsed[0] if parsed else ""
    os_id = values.get("ID", "").lower()
    version = values.get("VERSION_ID") or values.get("VERSION_CODENAME") or ""
    if os_id not in {"debian", "ubuntu"}:
        raise ScanError("unsupported_os", "guest OS is not Debian or Ubuntu")
    if not version:
        raise ScanError("unsupported_os", "guest OS version is unknown")
    return os_id, version


def _parse_apt_version(text: str) -> tuple[int, int, int]:
    """Parse the numeric feature-gated prefix of the APT version line.

    Only the numeric upstream ``major.minor.patch`` is gated; the full
    Debian/Ubuntu revision syntax (distro suffixes, architecture) is
    intentionally not validated here.
    """
    lines = text.splitlines()
    if not lines or not (match := APT_VERSION_RE.match(lines[0])):
        raise ScanError("execution_failed", "guest APT version output was malformed")
    return tuple(int(value) for value in match.groups())


def _package_failure(stage: str, stderr: str) -> ScanError:
    """Classify an APT command failure without exposing arbitrary output."""
    lowered = stderr.lower()
    if any(pattern in lowered for pattern in BUSY_PATTERNS):
        return ScanError("package_manager_busy", "APT or dpkg is busy")
    if stage == "metadata_refresh":
        return ScanError("metadata_refresh_failed", "APT metadata refresh failed")
    if stage == "mutation":
        return ScanError("mutation_failed", "APT package mutation failed")
    return ScanError("simulation_failed", "APT upgrade simulation failed")


def _read_inventory(
    runner: Runner, deadline: OperationDeadline, vmid: int
) -> str:
    """Read one bounded dpkg inventory through a fixed command."""
    inventory = _guest_command(runner, deadline, vmid, INVENTORY_COMMAND)
    installed_inventory, _ = _decode(inventory)
    if inventory.returncode != 0:
        raise ScanError("execution_failed", "could not read package inventory")
    return installed_inventory


def _validate_inventory_sane(installed_inventory: str) -> None:
    """Fail closed before/after mutation on malformed or unfinished dpkg state."""
    seen: set[tuple[str, str]] = set()
    for raw_line in installed_inventory.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split("\t")
        if len(fields) != 4 or fields[3] not in DPKG_STATUS_WORDS:
            raise ScanError("dpkg_sanity_failed", "dpkg inventory is malformed")
        name, architecture, version, status = fields
        if (
            not name
            or len(name) > 300
            or len(version) > 500
            or not ARCHITECTURE_RE.fullmatch(architecture)
        ):
            raise ScanError("dpkg_sanity_failed", "dpkg inventory is malformed")
        identity = (name, architecture)
        if identity in seen:
            raise ScanError("dpkg_sanity_failed", "dpkg inventory is malformed")
        seen.add(identity)
        if status in DPKG_UNFINISHED_STATUS_WORDS:
            raise ScanError("dpkg_unfinished", "dpkg reports unfinished package state")
        if status == "installed" and not version:
            raise ScanError("dpkg_sanity_failed", "dpkg inventory is malformed")


def _collect_plan(
    vmid: int, runner: Runner, deadline: OperationDeadline
) -> dict[str, str]:
    """Collect plan evidence after target validation."""
    simulation = _guest_command(
        runner,
        deadline,
        vmid,
        APT_SIMULATION_COMMAND,
        max_output=8 * 1024 * 1024,
    )
    simulation_stdout, simulation_stderr = _decode(simulation)
    if simulation.returncode != 0:
        raise _package_failure("simulation", simulation_stderr)

    architecture = _guest_command(
        runner,
        deadline,
        vmid,
        ("env", "LC_ALL=C", "dpkg", "--print-architecture"),
        max_output=4096,
    )
    native_architecture, _ = _decode(architecture)
    if architecture.returncode != 0:
        raise ScanError("execution_failed", "could not determine guest architecture")

    installed_inventory = _read_inventory(runner, deadline, vmid)
    return {
        "native_architecture": native_architecture,
        "installed_inventory": installed_inventory,
        "simulation": simulation_stdout,
    }


def _plan(
    expected_node: str, vmid: int, runner: Runner, deadline: OperationDeadline
) -> dict[str, str]:
    """Collect a fresh execution-time plan without refreshing APT metadata."""
    _validate_local_node(expected_node, runner, deadline)
    _validate_target(vmid, runner, deadline)
    return _collect_plan(vmid, runner, deadline)


def _update_packages(
    expected_node: str, vmid: int, runner: Runner, deadline: OperationDeadline
) -> dict[str, str]:
    """Run one fixed bare hardened upgrade and return before/after inventories."""
    _validate_local_node(expected_node, runner, deadline)
    _validate_target(vmid, runner, deadline)

    before = _read_inventory(runner, deadline, vmid)
    _validate_inventory_sane(before)

    mutation = _guest_command(
        runner,
        deadline,
        vmid,
        APT_MUTATION_COMMAND,
        command_timeout=UPDATE_COMMAND_TIMEOUT_SECONDS,
    )
    _, mutation_stderr = _decode(mutation)
    if mutation.returncode != 0:
        raise _package_failure("mutation", mutation_stderr)

    after = _read_inventory(runner, deadline, vmid)
    _validate_inventory_sane(after)
    return {"before_inventory": before, "after_inventory": after}


def _ping(
    expected_node: str, vmid: int, runner: Runner, deadline: OperationDeadline
) -> dict[str, bool]:
    """Ask one validated running LXC for a fixed trivial PONG."""
    _validate_local_node(expected_node, runner, deadline)
    _validate_target(vmid, runner, deadline)
    result = _guest_command(
        runner,
        deadline,
        vmid,
        ("/bin/true",),
        max_output=4096,
        command_timeout=PING_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ScanError("liveness_failed", "LXC did not answer the liveness probe")
    return {"pong": True}


def _scan(
    expected_node: str, vmid: int, runner: Runner, deadline: OperationDeadline
) -> dict[str, Any]:
    """Collect fixed package evidence or raise one bounded failure."""
    _validate_local_node(expected_node, runner, deadline)
    _validate_target(vmid, runner, deadline)

    os_result = _guest_command(
        runner,
        deadline,
        vmid,
        ("env", "LC_ALL=C", "cat", "/etc/os-release"),
        max_output=64 * 1024,
    )
    os_release, _ = _decode(os_result)
    if os_result.returncode != 0:
        raise ScanError("guest_unavailable", "guest OS metadata is unavailable")
    _parse_os_release(os_release)

    apt_version_result = _guest_command(
        runner,
        deadline,
        vmid,
        ("env", "LC_ALL=C", "apt-get", "--version"),
        max_output=64 * 1024,
    )
    apt_version, _ = _decode(apt_version_result)
    if apt_version_result.returncode != 0:
        raise ScanError("execution_failed", "could not determine guest APT version")
    if _parse_apt_version(apt_version) < MINIMUM_APT_VERSION:
        raise ScanError(
            "unsupported_os",
            "guest APT version does not support strict metadata refresh",
        )

    update = _guest_command(
        runner,
        deadline,
        vmid,
        (
            "env",
            "LC_ALL=C",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "update",
            "-qq",
            "--error-on=any",
        ),
    )
    _, update_stderr = _decode(update)
    if update.returncode != 0:
        raise _package_failure("metadata_refresh", update_stderr)

    plan = _collect_plan(vmid, runner, deadline)

    reboot = _guest_command(
        runner,
        deadline,
        vmid,
        ("test", "-e", "/var/run/reboot-required"),
        max_output=4096,
    )
    # Only the marker's presence is reliable evidence. Its absence (rc 1)
    # and any other outcome are both merely unknown, not a reliable
    # negative: no reboot-required marker package inference is made here.
    reboot_required = True if reboot.returncode == 0 else None
    return {
        "os_release": os_release,
        **plan,
        "reboot_required": reboot_required,
    }


def _response_base(operation: str, node: str, vmid: int) -> dict[str, Any]:
    """Return compatibility and identity metadata shared by every response."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "helper_version": HELPER_VERSION,
        "operation": operation,
        "target": {"node": node, "vmid": vmid},
    }


def handle_request(
    payload: Any,
    *,
    runner: Runner = _run_bounded,
    clock: Clock = time.monotonic,
    lock_factory: LockFactory = _target_lock,
) -> dict[str, Any]:
    """Perform the fixed, read/refresh-only package scan command sequence."""
    operation, expected_node, vmid = validate_request(payload)
    if operation == OPERATION_UPDATE_PACKAGES:
        operation_timeout = UPDATE_OPERATION_TIMEOUT_SECONDS
    elif operation == OPERATION_PING:
        operation_timeout = PING_OPERATION_TIMEOUT_SECONDS
    else:
        operation_timeout = OPERATION_TIMEOUT_SECONDS
    deadline = OperationDeadline(clock(), operation_timeout, clock)
    if operation == OPERATION_PROBE:
        try:
            node = _get_local_node(runner, deadline)
        except ScanError as err:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "helper_version": HELPER_VERSION,
                "operation": OPERATION_PROBE,
                "ok": False,
                "error": {
                    "classification": err.classification,
                    "message": err.message[:500],
                },
            }
        return {
            "protocol_version": PROTOCOL_VERSION,
            "helper_version": HELPER_VERSION,
            "operation": OPERATION_PROBE,
            "ok": True,
            "node": node,
        }
    assert expected_node is not None
    assert vmid is not None
    response = _response_base(operation, expected_node, vmid)
    try:
        with lock_factory(vmid):
            if operation == OPERATION_SCAN_PACKAGES:
                evidence = _scan(expected_node, vmid, runner, deadline)
            elif operation == OPERATION_PLAN_PACKAGES:
                evidence = _plan(expected_node, vmid, runner, deadline)
            elif operation == OPERATION_UPDATE_PACKAGES:
                evidence = _update_packages(expected_node, vmid, runner, deadline)
            else:
                evidence = _ping(expected_node, vmid, runner, deadline)
    except ScanError as err:
        return {
            **response,
            "ok": False,
            "error": {
                "classification": err.classification,
                "message": err.message[:500],
            },
        }
    return {**response, "ok": True, "evidence": evidence}


def _request_failure(
    message: str, operation: str = OPERATION_SCAN_PACKAGES
) -> dict[str, Any]:
    """Return a versioned response for input rejected before target validation."""
    response = {
        "protocol_version": PROTOCOL_VERSION,
        "helper_version": HELPER_VERSION,
        "operation": operation,
        "ok": False,
        "error": {
            "classification": "execution_failed",
            "message": message[:500],
        },
    }
    if operation != OPERATION_PROBE:
        response["target"] = {}
    return response


def main() -> int:
    """Read one bounded JSON request and emit one bounded JSON response."""
    error: str | None = None
    failure_operation = OPERATION_SCAN_PACKAGES
    if os.geteuid() != 0:
        error = "package scan helper must run as root"
    elif os.environ.get("SSH_ORIGINAL_COMMAND"):
        error = "remote command text is not accepted"
    else:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            error = "request exceeded its structural bound"
        else:
            try:
                payload = json.loads(raw.decode())
                if isinstance(payload, Mapping) and payload.get("operation") in {
                    OPERATION_PROBE,
                    OPERATION_SCAN_PACKAGES,
                    OPERATION_PLAN_PACKAGES,
                    OPERATION_UPDATE_PACKAGES,
                    OPERATION_PING,
                }:
                    failure_operation = payload["operation"]
                response = handle_request(payload)
                sys.stdout.write(json.dumps(response, separators=(",", ":")))
                return 0 if response["ok"] else 1
            except (UnicodeDecodeError, ValueError, RequestError) as err:
                error = str(err)[:500] or "malformed package-scan request"
    response = _request_failure(error or "malformed helper request", failure_operation)
    sys.stdout.write(json.dumps(response, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
