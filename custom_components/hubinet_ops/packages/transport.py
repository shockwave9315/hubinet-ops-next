"""Native async SSH transport for the forced-command package helper."""

import asyncio
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
import json
import logging
from pathlib import Path
import re
from typing import Any, Protocol

import asyncssh

from homeassistant.core import HomeAssistant

from .models import PackageScanError, PackageScanFailure, PackageScanResult
from .parser import PackageScanParseError, parse_apt_simulation, parse_os_release

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
HELPER_VERSION = 1
OPERATION_SCAN_PACKAGES = "scan_packages"

_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
_MAX_REQUEST_BYTES = 1024
_MAX_RESPONSE_BYTES = 48 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
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
    stderr: _SSHReader

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
        port: int = 22,
    ) -> None:
        """Initialize the transport with separate endpoint and identity inputs.

        ``port`` is not a product option; it is a fixed 22 in production and
        exists only so tests can point the real AsyncSSH client at a local
        in-process test server.
        """
        if not private_key_path.is_absolute() or not known_hosts_path.is_absolute():
            raise ValueError("package scan SSH trust paths must be absolute")
        self._endpoint = endpoint
        self._private_key_path = private_key_path
        self._known_hosts_path = known_hosts_path
        self._connector = connector
        self._port = port
        self._known_hosts_data: bytes | None = None
        self._ready = False

    @property
    def configured(self) -> bool:
        """Return whether trust material was ready at the last setup pass.

        This never touches the filesystem itself; it only reports the
        outcome cached by :meth:`async_prepare`. Trust material added or
        changed without a reload is intentionally not reflected here.
        """
        return self._ready

    async def async_prepare(self, hass: HomeAssistant) -> None:
        """Evaluate and cache local SSH trust material once, off the loop.

        Both required files are checked, and known_hosts content is read
        into memory here so :meth:`async_scan` never opens it itself; per
        the accepted architecture, readiness stays fixed until the next
        config-entry setup (reload).
        """
        self._known_hosts_data = await hass.async_add_executor_job(
            self._load_known_hosts
        )
        self._ready = self._known_hosts_data is not None

    def _load_known_hosts(self) -> bytes | None:
        """Blockingly check both trust files and read known_hosts content."""
        try:
            if not (
                self._private_key_path.is_file()
                and self._private_key_path.stat().st_size > 0
            ):
                return None
            if not (
                self._known_hosts_path.is_file()
                and self._known_hosts_path.stat().st_size > 0
            ):
                return None
            return self._known_hosts_path.read_bytes()
        except OSError:
            return None

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
                    port=self._port,
                    username="root",
                    config=None,
                    known_hosts=self._known_hosts_data,
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
                    # Only session/process kwargs belong here; create_process
                    # forwards unknown kwargs to create_session, which has no
                    # **kwargs sink and rejects connection-only options such
                    # as agent_forwarding (set above, at the connection level).
                    process = await connection.create_process(
                        input=encoded,
                        encoding=None,
                        request_pty=False,
                    )
                    stdout, stderr = await _drain_process_output(process)
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
            _LOGGER.debug(
                "package scan helper exited %s with bounded diagnostics: %r",
                completed.returncode,
                stderr[:200],
            )
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


async def _read_until_eof(
    reader: _SSHReader, *, max_bytes: int
) -> tuple[bytes, bool]:
    """Read one AsyncSSH stream to EOF, bounded, in a fixed chunk size.

    ``SSHReader.read(n)`` returns as soon as any data is available; it does
    not wait to fill ``n`` bytes or reach EOF. Multi-packet responses must be
    collected across repeated reads instead of a single call.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader.read(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > max_bytes:
            return b"", True
        chunks.append(chunk)


async def _drain_process_output(process: _SSHProcess) -> tuple[bytes, bytes]:
    """Drain stdout and stderr concurrently, terminating on either bound.

    stdout and stderr share the same underlying SSH channel receive window
    (RFC 4254 extended data uses the same window accounting as normal
    data). If one stream stops being read after exceeding its bound while
    the other is still awaited to EOF, the still-active reader can stall
    forever on window-blocked data that will never arrive. The process is
    therefore killed as soon as either bound is exceeded, not only once
    both readers finish -- so the 300-second outer transport timeout is
    never the thing that catches this.
    """
    stdout_task = asyncio.ensure_future(
        _read_until_eof(process.stdout, max_bytes=_MAX_RESPONSE_BYTES)
    )
    stderr_task = asyncio.ensure_future(
        _read_until_eof(process.stderr, max_bytes=_MAX_STDERR_BYTES)
    )
    pending = {stdout_task, stderr_task}
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            if any(task.result()[1] for task in done):
                process.kill()
                await process.wait_closed()
                raise PackageScanError(
                    PackageScanFailure.EXECUTION_FAILED,
                    "package scan helper output exceeded its bound",
                )
    finally:
        # Any task not yet done here (the peer that never reached its own
        # bound or EOF) must not be left to complete on its own; cancel it
        # and absorb its CancelledError so it is never a leaked exception.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return stdout_task.result()[0], stderr_task.result()[0]


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
        type(payload.get("protocol_version")) is not int
        or payload.get("protocol_version") != PROTOCOL_VERSION
        or type(payload.get("helper_version")) is not int
        or payload.get("helper_version") != HELPER_VERSION
        or payload.get("operation") != OPERATION_SCAN_PACKAGES
    ):
        raise PackageScanError(
            PackageScanFailure.PROTOCOL_MISMATCH,
            "package scan helper protocol is incompatible",
        )

    target = payload.get("target")
    target_matches = (
        isinstance(target, Mapping)
        and bool(target)
        and type(target.get("vmid")) is int
        and target.get("node") == expected_node
        and target.get("vmid") == expected_vmid
    )

    if payload.get("ok") is True:
        # Success is only ever accepted for the exact requested identity.
        if not target_matches:
            raise PackageScanError(
                PackageScanFailure.IDENTITY_MISMATCH,
                "package scan helper returned the wrong node or LXC identity",
            )
    else:
        error = payload.get("error")
        if not isinstance(error, Mapping):
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan helper returned an unclassified failure",
            )
        # A failure with a validated target must also match identity. A
        # failure raised before the helper could validate any target (an
        # empty/absent target) carries no identity claim to check, and must
        # surface its real classification instead of a manufactured
        # identity mismatch.
        if isinstance(target, Mapping) and target and not target_matches:
            raise PackageScanError(
                PackageScanFailure.IDENTITY_MISMATCH,
                "package scan helper returned the wrong node or LXC identity",
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
