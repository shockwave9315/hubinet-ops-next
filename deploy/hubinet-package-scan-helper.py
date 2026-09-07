#!/usr/bin/env python3
"""Forced-command PVE boundary for the sole package-scan operation."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
import re
import selectors
import shlex
import subprocess
import sys
import time
from typing import Any

MAX_REQUEST_BYTES = 1024
MAX_COMMAND_OUTPUT_BYTES = 16 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 120.0
APT_VERSION_RE = re.compile(
    r"apt ([0-9]+)\.([0-9]+)\.([0-9]+)"
    r"(?:[~+.-][A-Za-z0-9.+:~-]*)? \([A-Za-z0-9_-]+\)"
)
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


type Runner = Callable[[tuple[str, ...], float, int], CommandResult]


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


def _run_bounded(
    argv: tuple[str, ...], timeout: float, max_output: int
) -> CommandResult:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    started = time.monotonic()
    timed_out = output_exceeded = False
    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                if len(output["stdout"]) + len(output["stderr"]) > max_output:
                    output_exceeded = True
                    process.kill()
                    break
            if output_exceeded:
                break
    finally:
        selector.close()
        process.wait(timeout=5)
    return CommandResult(
        process.returncode,
        bytes(output["stdout"][: max_output + 1]),
        bytes(output["stderr"][: max_output + 1]),
        timed_out,
        output_exceeded,
    )


def validate_request(payload: Any) -> int:
    """Validate the sole accepted operation and return its VMID."""
    if not isinstance(payload, Mapping) or set(payload) != {
        "request_version",
        "operation",
        "target",
    }:
        raise RequestError("request must have the exact package-scan shape")
    if payload["request_version"] != 1 or payload["operation"] != "scan_packages":
        raise RequestError("unknown host-control operation")
    target = payload["target"]
    if not isinstance(target, Mapping) or set(target) != {"vmid"}:
        raise RequestError("target must have the exact package-scan shape")
    vmid = target["vmid"]
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid must be a valid PVE integer VMID")
    return vmid


def _command(
    runner: Runner, argv: tuple[str, ...], *, max_output: int = MAX_COMMAND_OUTPUT_BYTES
) -> CommandResult:
    result = runner(argv, COMMAND_TIMEOUT_SECONDS, max_output)
    if result.timed_out:
        raise ScanError("timeout", "package scan command timed out")
    if result.output_exceeded:
        raise ScanError("execution_failed", "package scan output exceeded its bound")
    return result


def _guest_command(
    runner: Runner,
    vmid: int,
    tail: tuple[str, ...],
    *,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    return _command(
        runner, ("pct", "exec", str(vmid), "--", *tail), max_output=max_output
    )


def _decode(result: CommandResult) -> tuple[str, str]:
    try:
        return result.stdout.decode(), result.stderr.decode()
    except UnicodeDecodeError as err:
        raise ScanError(
            "execution_failed", "package scan command output was not UTF-8"
        ) from err


def _parse_os_release(text: str) -> tuple[str, str]:
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
    lines = text.splitlines()
    if not lines or not (match := APT_VERSION_RE.fullmatch(lines[0])):
        raise ScanError("execution_failed", "guest APT version output was malformed")
    return tuple(int(value) for value in match.groups())


def _package_failure(stage: str, stderr: str) -> ScanError:
    lowered = stderr.lower()
    if any(pattern in lowered for pattern in BUSY_PATTERNS):
        return ScanError("package_manager_busy", "APT or dpkg is busy")
    if stage == "metadata_refresh":
        return ScanError("metadata_refresh_failed", "APT metadata refresh failed")
    return ScanError("simulation_failed", "APT upgrade simulation failed")


def _scan(vmid: int, runner: Runner) -> dict[str, Any]:
    """Collect fixed package evidence or raise one bounded failure."""
    os_result = _guest_command(
        runner,
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
        vmid,
        ("env", "LC_ALL=C", "dpkg", "--print-architecture"),
        max_output=4096,
    )
    native_architecture, _ = _decode(architecture)
    if architecture.returncode != 0:
        raise ScanError("execution_failed", "could not determine guest architecture")

    inventory = _guest_command(
        runner,
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
        vmid,
        ("test", "-e", "/var/run/reboot-required"),
        max_output=4096,
    )
    if reboot.returncode not in {0, 1}:
        raise ScanError("execution_failed", "could not read reboot evidence")
    return {
        "os_release": os_release,
        "native_architecture": native_architecture,
        "installed_inventory": installed_inventory,
        "simulation": simulation_stdout,
        "reboot_required": reboot.returncode == 0,
    }


def handle_request(payload: Any, *, runner: Runner = _run_bounded) -> dict[str, Any]:
    """Perform the fixed, read/refresh-only package scan command sequence."""
    vmid = validate_request(payload)
    target = {"vmid": vmid}
    try:
        evidence = _scan(vmid, runner)
    except ScanError as err:
        return {
            "response_version": 1,
            "ok": False,
            "target": target,
            "error": {
                "classification": err.classification,
                "message": err.message[:500],
            },
        }
    return {
        "response_version": 1,
        "ok": True,
        "target": target,
        "evidence": evidence,
    }


def main() -> int:
    """Read one bounded JSON request and emit one bounded JSON response."""
    if os.environ.get("SSH_ORIGINAL_COMMAND"):
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
    response = {
        "response_version": 1,
        "ok": False,
        "target": {},
        "error": {"classification": "execution_failed", "message": error},
    }
    sys.stdout.write(json.dumps(response, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
