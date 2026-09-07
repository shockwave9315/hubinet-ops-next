"""Bounded SSH invocation of the package-scan host helper."""

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from typing import Any

from .models import PackageScanError, PackageScanFailure, PackageScanResult
from .parser import PackageScanParseError, parse_apt_simulation, parse_os_release

_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
_MAX_REQUEST_BYTES = 1024
_MAX_RESPONSE_BYTES = 48 * 1024 * 1024
_TIMEOUT_SECONDS = 300.0
_CLEANUP_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded subprocess result."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False


type ProcessRunner = Callable[[tuple[str, ...], bytes, float, int], ProcessResult]


def _run_bounded(
    argv: tuple[str, ...], input_bytes: bytes, timeout: float, max_output: int
) -> ProcessResult:
    """Run a process with bounded input, time, and combined output."""
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    pending = memoryview(input_bytes)
    stdin_open = bool(pending)
    if stdin_open:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    timed_out = output_exceeded = False
    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                if key.data == "stdin":
                    try:
                        written = os.write(key.fileobj.fileno(), pending)
                    except BlockingIOError:
                        continue
                    except OSError:
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                        stdin_open = False
                        continue
                    pending = pending[written:]
                    if not pending:
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                        stdin_open = False
                    continue
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
        if stdin_open:
            with suppress(OSError):
                process.stdin.close()
        process.wait(timeout=_CLEANUP_SECONDS)
    return ProcessResult(
        process.returncode,
        bytes(output["stdout"][: max_output + 1]),
        bytes(output["stderr"][: max_output + 1]),
        timed_out,
        output_exceeded,
    )


class PackageScanner:
    """Call the forced-command helper on an upstream-discovered PVE node."""

    def __init__(
        self,
        *,
        private_key_path: Path,
        known_hosts_path: Path,
        runner: ProcessRunner = _run_bounded,
    ) -> None:
        """Initialize the scanner with explicit SSH trust files."""
        if not private_key_path.is_absolute() or not known_hosts_path.is_absolute():
            raise ValueError("package scan SSH trust paths must be absolute")
        self._private_key_path = private_key_path
        self._known_hosts_path = known_hosts_path
        self._runner = runner

    def scan(self, node: str, vmid: int) -> PackageScanResult:
        """Scan one LXC identity supplied by the upstream coordinator."""
        if not _HOST_RE.fullmatch(node):
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "Proxmox node name is invalid for package scanning",
            )
        if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "Proxmox LXC VMID is invalid for package scanning",
            )
        request = {
            "request_version": 1,
            "operation": "scan_packages",
            "target": {"vmid": vmid},
        }
        encoded = json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan request exceeded its bound",
            )
        argv = (
            "ssh",
            "-T",
            "-i",
            str(self._private_key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self._known_hosts_path}",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ClearAllForwardings=yes",
            f"root@{node}",
        )
        result = self._runner(argv, encoded, _TIMEOUT_SECONDS, _MAX_RESPONSE_BYTES)
        if result.timed_out:
            raise PackageScanError(
                PackageScanFailure.TIMEOUT, "package scan helper timed out"
            )
        if result.output_exceeded:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan helper output exceeded its bound",
            )
        if result.returncode != 0 and not result.stdout:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan SSH execution failed",
            )
        try:
            payload = json.loads(result.stdout.decode())
        except (UnicodeDecodeError, ValueError) as err:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan helper returned a malformed response",
            ) from err
        return _parse_response(payload, vmid)


def _parse_response(payload: Any, expected_vmid: int) -> PackageScanResult:
    """Validate helper output and parse its package evidence."""
    if not isinstance(payload, Mapping) or payload.get("response_version") != 1:
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned an unsupported response",
        )
    target = payload.get("target")
    if not isinstance(target, Mapping) or target.get("vmid") != expected_vmid:
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned the wrong LXC identity",
        )
    if payload.get("ok") is not True:
        error = payload.get("error")
        if not isinstance(error, Mapping):
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan helper returned an unclassified failure",
            )
        try:
            failure = PackageScanFailure(str(error["classification"]))
        except (KeyError, ValueError) as err:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan helper returned an unknown failure",
            ) from err
        raise PackageScanError(
            failure, str(error.get("message") or "package scan failed")[:500]
        )
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned malformed evidence",
        )
    reboot_required = evidence.get("reboot_required")
    if type(reboot_required) is not bool:
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned malformed reboot evidence",
        )
    try:
        os_id, os_version = parse_os_release(str(evidence["os_release"]))
        packages = parse_apt_simulation(
            str(evidence["simulation"]),
            native_architecture=str(evidence["native_architecture"]),
            installed_inventory=str(evidence["installed_inventory"]),
        )
    except KeyError as err:
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned incomplete evidence",
        ) from err
    except PackageScanParseError as err:
        raise PackageScanError(PackageScanFailure.MALFORMED_PLAN, str(err)) from err
    return PackageScanResult(os_id, os_version, packages, reboot_required)
