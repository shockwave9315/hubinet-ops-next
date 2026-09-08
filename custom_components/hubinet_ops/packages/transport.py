"""Native async SSH transport for the forced-command package helper."""

import asyncio
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
import json
from pathlib import Path
import re
from typing import Any, Protocol

import asyncssh

from .models import PackageScanError, PackageScanFailure, PackageScanResult
from .parser import PackageScanParseError, parse_apt_simulation, parse_os_release

PROTOCOL_VERSION = 1
HELPER_VERSION = 1
OPERATION_SCAN_PACKAGES = "scan_packages"

_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
_MAX_REQUEST_BYTES = 1024
_MAX_RESPONSE_BYTES = 48 * 1024 * 1024
TRANSPORT_TIMEOUT_SECONDS = 300.0


class _SSHReader(Protocol):
    """Subset of the merged AsyncSSH output stream used by the transport."""

    async def read(self, size: int = -1) -> bytes:
        """Read no more than the requested bytes."""


class _SSHCompletedProcess(Protocol):
    """Subset of AsyncSSH completion evidence used by the transport."""

    returncode: int | None


class _SSHProcess(Protocol):
    """Subset of an AsyncSSH client process used by the transport."""

    stdout: _SSHReader

    async def wait(self, check: bool = False) -> _SSHCompletedProcess:
        """Wait for process completion without raising for a nonzero exit."""

    def kill(self) -> None:
        """Stop a process which exceeded its output bound."""

    async def wait_closed(self) -> None:
        """Wait for a killed process channel to close."""


class _SSHConnection(Protocol):
    """Subset of an AsyncSSH client connection used by the transport."""

    async def create_process(self, *args: object, **kwargs: object) -> _SSHProcess:
        """Open the forced-command session."""


class _SSHConnector(Protocol):
    """AsyncSSH-compatible connection factory."""

    def __call__(
        self, *args: object, **kwargs: object
    ) -> AbstractAsyncContextManager[_SSHConnection]:
        """Return an async SSH connection context manager."""


class AsyncSSHPackageTransport:
    """Call the root-owned, forced-command helper using one explicit SSH key."""

    def __init__(
        self,
        *,
        endpoint: str,
        private_key_path: Path,
        known_hosts_path: Path,
        connector: _SSHConnector = asyncssh.connect,
    ) -> None:
        """Initialize the transport with separate endpoint and identity inputs."""
        if not private_key_path.is_absolute() or not known_hosts_path.is_absolute():
            raise ValueError("package scan SSH trust paths must be absolute")
        self._endpoint = endpoint
        self._private_key_path = private_key_path
        self._known_hosts_path = known_hosts_path
        self._connector = connector

    @property
    def configured(self) -> bool:
        """Return whether both required local SSH trust files are present."""
        try:
            return (
                self._private_key_path.is_file()
                and self._private_key_path.stat().st_size > 0
                and self._known_hosts_path.is_file()
                and self._known_hosts_path.stat().st_size > 0
            )
        except OSError:
            return False

    async def async_scan(self, expected_node: str, vmid: int) -> PackageScanResult:
        """Scan one upstream-discovered LXC through the configured API endpoint."""
        _validate_target(expected_node, vmid)
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": OPERATION_SCAN_PACKAGES,
            "target": {"node": expected_node, "vmid": vmid},
        }
        encoded = json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan request exceeded its bound",
            )

        try:
            async with asyncio.timeout(TRANSPORT_TIMEOUT_SECONDS):
                async with self._connector(
                    self._endpoint,
                    port=22,
                    username="root",
                    config=None,
                    known_hosts=str(self._known_hosts_path),
                    client_keys=[str(self._private_key_path)],
                    agent_path=None,
                    pkcs11_provider=None,
                    password=None,
                    password_auth=False,
                    kbdint_auth=False,
                    host_based_auth=False,
                    gss_auth=False,
                    gss_kex=False,
                    preferred_auth=("publickey",),
                    public_key_auth=True,
                    client_host_keysign=False,
                    agent_forwarding=False,
                    disable_trivial_auth=True,
                ) as connection:
                    process = await connection.create_process(
                        input=encoded,
                        encoding=None,
                        request_pty=False,
                        agent_forwarding=False,
                        stderr=asyncssh.STDOUT,
                    )
                    stdout = await process.stdout.read(_MAX_RESPONSE_BYTES + 1)
                    if len(stdout) > _MAX_RESPONSE_BYTES:
                        process.kill()
                        await process.wait_closed()
                        raise PackageScanError(
                            PackageScanFailure.EXECUTION_FAILED,
                            "package scan helper output exceeded its bound",
                        )
                    completed = await process.wait()
        except TimeoutError as err:
            raise PackageScanError(
                PackageScanFailure.TIMEOUT, "package scan helper timed out"
            ) from err
        except (asyncssh.Error, OSError, ValueError) as err:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan SSH execution failed",
            ) from err

        if not isinstance(stdout, bytes):
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan helper returned invalid output",
            )
        if completed.returncode != 0 and not stdout:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan SSH execution failed",
            )
        try:
            payload = json.loads(stdout.decode())
        except (UnicodeDecodeError, ValueError) as err:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan helper returned a malformed response",
            ) from err
        return _parse_response(payload, expected_node, vmid)


def _validate_target(expected_node: str, vmid: int) -> None:
    """Validate identity fields without treating the node as an endpoint."""
    if not _NODE_RE.fullmatch(expected_node):
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "Proxmox node identity is invalid for package scanning",
        )
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "Proxmox LXC VMID is invalid for package scanning",
        )


def _parse_response(
    payload: Any, expected_node: str, expected_vmid: int
) -> PackageScanResult:
    """Validate helper metadata, target identity, and package evidence."""
    if not isinstance(payload, Mapping):
        raise PackageScanError(
            PackageScanFailure.PROTOCOL_MISMATCH,
            "package scan helper returned a malformed protocol response",
        )
    if (
        payload.get("protocol_version") != PROTOCOL_VERSION
        or payload.get("helper_version") != HELPER_VERSION
        or payload.get("operation") != OPERATION_SCAN_PACKAGES
    ):
        raise PackageScanError(
            PackageScanFailure.PROTOCOL_MISMATCH,
            "package scan helper protocol is incompatible",
        )
    target = payload.get("target")
    if (
        not isinstance(target, Mapping)
        or target.get("node") != expected_node
        or target.get("vmid") != expected_vmid
    ):
        raise PackageScanError(
            PackageScanFailure.IDENTITY_MISMATCH,
            "package scan helper returned the wrong node or LXC identity",
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
        message = error.get("message")
        raise PackageScanError(
            failure,
            str(message)[:500] if message else "package scan failed",
        )

    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned malformed evidence",
        )
    reboot_required = evidence.get("reboot_required")
    if reboot_required is not None and type(reboot_required) is not bool:
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned malformed reboot evidence",
        )
    try:
        os_release = evidence["os_release"]
        native_architecture = evidence["native_architecture"]
        installed_inventory = evidence["installed_inventory"]
        simulation = evidence["simulation"]
    except KeyError as err:
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned incomplete evidence",
        ) from err
    if not all(
        isinstance(value, str)
        for value in (
            os_release,
            native_architecture,
            installed_inventory,
            simulation,
        )
    ):
        raise PackageScanError(
            PackageScanFailure.EXECUTION_FAILED,
            "package scan helper returned malformed evidence",
        )
    try:
        os_id, os_version = parse_os_release(os_release)
        parsed = parse_apt_simulation(
            simulation,
            native_architecture=native_architecture,
            installed_inventory=installed_inventory,
        )
    except PackageScanParseError as err:
        raise PackageScanError(err.failure, str(err)) from err
    return PackageScanResult(
        os_id=os_id,
        os_version=os_version,
        packages=parsed.packages,
        reboot_required=reboot_required,
        not_upgraded_count=parsed.not_upgraded_count,
    )
