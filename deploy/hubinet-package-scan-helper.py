#!/usr/bin/env python3
"""Root-owned forced-command PVE boundary for package scans only."""

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
HELPER_VERSION = 1
OPERATION = "scan_packages"

MAX_REQUEST_BYTES = 1024
MAX_COMMAND_OUTPUT_BYTES = 16 * 1024 * 1024
OPERATION_TIMEOUT_SECONDS = 240.0
COMMAND_TIMEOUT_SECONDS = 120.0
LOCK_DIRECTORY = "/run/lock"
PVE_LOCAL_NODE_LINK = "/etc/pve/local"
PVE_LOCAL_NODE_PREFIX = "/etc/pve/nodes/"

NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
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
                # look like a timeout.
                for key, _ in selector.select(timeout=0):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if chunk:
                        output[key.data].extend(chunk)
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                if len(output["stdout"]) + len(output["stderr"]) > max_output:
                    output_exceeded = True
                    break
            if output_exceeded:
                break
    finally:
        selector.close()
        if timed_out or output_exceeded:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(ProcessLookupError):
                process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Cleanup itself must never raise past this function: a stuck
            # or unreapable child becomes a structured timeout instead of
            # an uncaught traceback that would bypass the JSON protocol.
            cleanup_failed = True
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


def validate_request(payload: Any) -> tuple[str, int]:
    """Validate the sole accepted operation and return target identity."""
    if not isinstance(payload, Mapping) or set(payload) != {
        "protocol_version",
        "operation",
        "target",
    }:
        raise RequestError("request must have the exact package-scan shape")
    if (
        type(payload["protocol_version"]) is not int
        or payload["protocol_version"] != PROTOCOL_VERSION
    ):
        raise RequestError("unsupported package-scan protocol version")
    if payload["operation"] != OPERATION:
        raise RequestError("unknown host-control operation")
    target = payload["target"]
    if not isinstance(target, Mapping) or set(target) != {"node", "vmid"}:
        raise RequestError("target must have the exact package-scan shape")
    node = target["node"]
    vmid = target["vmid"]
    if not isinstance(node, str) or not NODE_RE.fullmatch(node):
        raise RequestError("node must be a valid PVE node identity")
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid must be a valid PVE integer VMID")
    return node, vmid


def _command(
    runner: Runner,
    deadline: OperationDeadline,
    argv: tuple[str, ...],
    *,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    """Run one command using no more than the global remaining time."""
    remaining = deadline.remaining()
    if remaining <= 0:
        raise ScanError("timeout", "package scan operation deadline exceeded")
    result = runner(argv, min(COMMAND_TIMEOUT_SECONDS, remaining), max_output)
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
) -> CommandResult:
    """Run one fixed command in the target LXC."""
    return _command(
        runner,
        deadline,
        ("pct", "exec", str(vmid), "--", *tail),
        max_output=max_output,
    )


def _decode(result: CommandResult) -> tuple[str, str]:
    """Decode bounded command output as strict UTF-8."""
    try:
        return result.stdout.decode(), result.stderr.decode()
    except UnicodeDecodeError as err:
        raise ScanError(
            "execution_failed", "package scan command output was not UTF-8"
        ) from err


def _validate_local_node(
    expected_node: str, runner: Runner, deadline: OperationDeadline
) -> None:
    """Verify the expected PVE node against PVE-native local identity.

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
    return ScanError("simulation_failed", "APT upgrade simulation failed")


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

    simulation = _guest_command(
        runner,
        deadline,
        vmid,
        (
            "env",
            "LC_ALL=C",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "-s",
            "upgrade",
        ),
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

    inventory = _guest_command(
        runner,
        deadline,
        vmid,
        (
            "env",
            "LC_ALL=C",
            "dpkg-query",
            "-W",
            "-f=${Package}\\t${Architecture}\\t${Version}\\t${db:Status-Status}\\n",
        ),
    )
    installed_inventory, _ = _decode(inventory)
    if inventory.returncode != 0:
        raise ScanError("execution_failed", "could not read package inventory")

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
        "native_architecture": native_architecture,
        "installed_inventory": installed_inventory,
        "simulation": simulation_stdout,
        "reboot_required": reboot_required,
    }


def _response_base(node: str, vmid: int) -> dict[str, Any]:
    """Return compatibility and identity metadata shared by every response."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "helper_version": HELPER_VERSION,
        "operation": OPERATION,
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
    expected_node, vmid = validate_request(payload)
    response = _response_base(expected_node, vmid)
    deadline = OperationDeadline(clock(), OPERATION_TIMEOUT_SECONDS, clock)
    try:
        with lock_factory(vmid):
            evidence = _scan(expected_node, vmid, runner, deadline)
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


def _request_failure(message: str) -> dict[str, Any]:
    """Return a versioned response for input rejected before target validation."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "helper_version": HELPER_VERSION,
        "operation": OPERATION,
        "target": {},
        "ok": False,
        "error": {
            "classification": "execution_failed",
            "message": message[:500],
        },
    }


def main() -> int:
    """Read one bounded JSON request and emit one bounded JSON response."""
    error: str | None = None
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
                response = handle_request(json.loads(raw.decode()))
                sys.stdout.write(json.dumps(response, separators=(",", ":")))
                return 0 if response["ok"] else 1
            except (UnicodeDecodeError, ValueError, RequestError) as err:
                error = str(err)[:500] or "malformed package-scan request"
    response = _request_failure(error or "malformed package-scan request")
    sys.stdout.write(json.dumps(response, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
