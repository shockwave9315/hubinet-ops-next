"""Native async SSH transport for the forced-command package helper."""

import asyncio
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Protocol

import asyncssh

from homeassistant.core import HomeAssistant

from .models import (
    PackageMutationResult,
    PackageScanError,
    PackageScanFailure,
    PackageScanResult,
    PackageUpdateError,
    PackageUpdateOutcome,
)
from .parser import (
    PackageScanParseError,
    ParsedAptSimulation,
    ParsedAutoremoveSimulation,
    parse_apt_simulation,
    parse_autoremove_simulation,
    parse_installed_inventory,
    parse_os_release,
)

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
OPERATION_PROBE = "probe"
OPERATION_SCAN_PACKAGES = "scan_packages"
OPERATION_PLAN_PACKAGES = "plan_packages"
OPERATION_UPDATE_PACKAGES = "update_packages"
OPERATION_PLAN_AUTOREMOVE = "plan_autoremove"
OPERATION_AUTOREMOVE_PACKAGES = "autoremove_packages"
OPERATION_PING = "ping"

_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
_MAX_REQUEST_BYTES = 1024
_MAX_RESPONSE_BYTES = 48 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
TRANSPORT_TIMEOUT_SECONDS = 300.0
UPDATE_TRANSPORT_TIMEOUT_SECONDS = 1860.0
PING_TRANSPORT_TIMEOUT_SECONDS = 45.0
PROBE_TIMEOUT_SECONDS = 30.0


def _build_known_hosts(endpoint: str, host_key: str, port: int = 22) -> bytes:
    """Bind one enrolled public host key to the user-entered endpoint."""
    if not endpoint or any(char.isspace() for char in endpoint) or "," in endpoint:
        raise ValueError("PVE SSH endpoint is invalid")
    if "\n" in host_key or "\r" in host_key:
        raise ValueError("PVE SSH host key must contain exactly one key")
    host_pattern = endpoint if port == 22 else f"[{endpoint}]:{port}"
    return f"{host_pattern} {host_key}\n".encode("ascii")


class PackageTransportConnectionError(Exception):
    """The SSH endpoint could not be reached."""


class PackageTransportTimeoutError(PackageTransportConnectionError):
    """The SSH request exceeded its bounded deadline."""


class PackageTransportAuthenticationError(Exception):
    """The enrolled SSH key was not accepted."""


class PackageTransportHostKeyError(Exception):
    """The SSH endpoint did not present the enrolled host key."""


class PackageHelperUnavailableError(Exception):
    """The forced-command helper is missing or does not support probing."""


class PackageHelperProtocolError(Exception):
    """The helper returned an incompatible or malformed protocol response."""


@dataclass(frozen=True, slots=True)
class PackageHelperProbe:
    """Authenticated identity returned by the local PVE helper."""

    node: str
    helper_version: int


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
        """Send a remote-signal kill request (best-effort; peer may ignore it)."""

    def close(self) -> None:
        """Locally close the channel's send/recv state, independent of the peer."""

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
        private_key: str | None,
        host_key: str | None,
        connector: _SSHConnector = asyncssh.connect,
        port: int = 22,
    ) -> None:
        """Initialize the transport with separate endpoint and identity inputs.

        ``port`` is not a product option; it is a fixed 22 in production and
        exists only so tests can point the real AsyncSSH client at a local
        in-process test server.
        """
        self._endpoint = endpoint
        self._connector = connector
        self._port = port
        self._client_key: asyncssh.SSHKey | None = None
        self._known_hosts_data: bytes | None = None
        self._observed_helper_version: int | None = None
        if private_key and host_key:
            self._client_key = asyncssh.import_private_key(private_key.encode())
            if self._client_key.get_algorithm() != "ssh-ed25519":
                raise ValueError("package SSH private key must be Ed25519")
            imported_host_key = asyncssh.import_public_key(host_key.encode())
            if imported_host_key.get_algorithm() != "ssh-ed25519":
                raise ValueError("package SSH host key must be Ed25519")
            canonical_host_key = imported_host_key.export_public_key().decode().strip()
            self._known_hosts_data = _build_known_hosts(
                endpoint, canonical_host_key, port
            )

    @property
    def configured(self) -> bool:
        """Return whether both in-memory trust inputs are present."""
        return self._client_key is not None and self._known_hosts_data is not None

    @property
    def observed_helper_version(self) -> int | None:
        """Return the latest structurally valid helper version seen on the wire."""
        return self._observed_helper_version

    async def async_prepare(self, hass: HomeAssistant) -> None:
        """Retain the package-manager setup hook; no filesystem I/O is needed."""

    def _require_configured(self) -> None:
        """Fail closed before connecting when either trust input is absent."""
        if not self.configured:
            raise PackageTransportConnectionError(
                "package SSH trust material is not configured"
            )

    async def async_probe(self) -> PackageHelperProbe:
        """Prove SSH trust, forced-command routing, protocol, and local node."""
        payload = await self._async_request(
            {
                "protocol_version": PROTOCOL_VERSION,
                "operation": OPERATION_PROBE,
            },
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if not isinstance(payload, Mapping):
            raise PackageHelperProtocolError("helper returned malformed probe data")
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise PackageHelperProtocolError("helper protocol is incompatible")
        helper_version = payload.get("helper_version")
        if type(helper_version) is not int or helper_version < 1:
            raise PackageHelperProtocolError("helper version metadata is malformed")
        if payload.get("operation") != OPERATION_PROBE:
            raise PackageHelperUnavailableError(
                "helper is missing or outdated; run the bootstrap command again"
            )
        node = payload.get("node")
        if payload.get("ok") is not True or not isinstance(node, str):
            raise PackageHelperUnavailableError(
                "helper probe failed; run the bootstrap command again"
            )
        if not _NODE_RE.fullmatch(node):
            raise PackageHelperProtocolError("helper returned an invalid PVE node")
        return PackageHelperProbe(node=node, helper_version=helper_version)

    async def async_scan(self, expected_node: str, vmid: int) -> PackageScanResult:
        """Scan one upstream-discovered LXC through the configured API endpoint."""
        _validate_target(expected_node, vmid)
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": OPERATION_SCAN_PACKAGES,
            "target": {"node": expected_node, "vmid": vmid},
        }
        try:
            payload = await self._async_request(
                request, timeout=TRANSPORT_TIMEOUT_SECONDS
            )
        except PackageTransportTimeoutError as err:
            raise PackageScanError(
                PackageScanFailure.TIMEOUT, "package scan helper timed out"
            ) from err
        except PackageTransportHostKeyError as err:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan SSH host key did not match",
            ) from err
        except PackageTransportAuthenticationError as err:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED,
                "package scan SSH authentication failed",
            ) from err
        except (
            PackageTransportConnectionError,
            PackageHelperUnavailableError,
            PackageHelperProtocolError,
        ) as err:
            raise PackageScanError(
                PackageScanFailure.EXECUTION_FAILED, str(err)
            ) from err
        return _parse_response(payload, expected_node, vmid)

    async def async_plan(
        self, expected_node: str, vmid: int
    ) -> ParsedAptSimulation:
        """Return a fresh hardened simulation without refreshing APT metadata."""
        payload = await self._async_update_request(
            expected_node,
            vmid,
            OPERATION_PLAN_PACKAGES,
            timeout=TRANSPORT_TIMEOUT_SECONDS,
            timeout_outcome=PackageUpdateOutcome.PLAN_FAILED,
            connection_outcome=PackageUpdateOutcome.PLAN_FAILED,
        )
        evidence = _parse_update_response(
            payload, expected_node, vmid, OPERATION_PLAN_PACKAGES
        )
        try:
            native_architecture = evidence["native_architecture"]
            installed_inventory = evidence["installed_inventory"]
            simulation = evidence["simulation"]
        except KeyError as err:
            raise PackageUpdateError(
                PackageUpdateOutcome.PLAN_FAILED,
                "package plan helper returned incomplete evidence",
            ) from err
        if not all(
            isinstance(value, str)
            for value in (native_architecture, installed_inventory, simulation)
        ):
            raise PackageUpdateError(
                PackageUpdateOutcome.PLAN_FAILED,
                "package plan helper returned malformed evidence",
            )
        try:
            return parse_apt_simulation(
                simulation,
                native_architecture=native_architecture,
                installed_inventory=installed_inventory,
            )
        except PackageScanParseError as err:
            raise PackageUpdateError(
                PackageUpdateOutcome.PLAN_FAILED, str(err)
            ) from err

    async def async_update(
        self, expected_node: str, vmid: int
    ) -> PackageMutationResult:
        """Run one fixed bare upgrade and parse its before/after inventories."""
        payload = await self._async_update_request(
            expected_node,
            vmid,
            OPERATION_UPDATE_PACKAGES,
            timeout=UPDATE_TRANSPORT_TIMEOUT_SECONDS,
            timeout_outcome=PackageUpdateOutcome.MUTATION_TIMED_OUT,
            connection_outcome=PackageUpdateOutcome.MUTATION_UNCERTAIN,
        )
        evidence = _parse_update_response(
            payload, expected_node, vmid, OPERATION_UPDATE_PACKAGES
        )
        try:
            before_text = evidence["before_inventory"]
            after_text = evidence["after_inventory"]
        except KeyError as err:
            raise PackageUpdateError(
                PackageUpdateOutcome.MUTATION_UNCERTAIN,
                "package update helper returned incomplete inventory evidence",
            ) from err
        if not isinstance(before_text, str) or not isinstance(after_text, str):
            raise PackageUpdateError(
                PackageUpdateOutcome.MUTATION_UNCERTAIN,
                "package update helper returned malformed inventory evidence",
            )
        try:
            before = parse_installed_inventory(before_text)
            after = parse_installed_inventory(after_text)
        except PackageScanParseError as err:
            raise PackageUpdateError(
                PackageUpdateOutcome.MUTATION_FAILED,
                "post-update package inventory could not be parsed",
            ) from err
        if before.unfinished or after.unfinished:
            raise PackageUpdateError(
                PackageUpdateOutcome.MUTATION_FAILED,
                "dpkg reports unfinished package state after update",
            )
        return PackageMutationResult(before=before.installed, after=after.installed)

    async def async_plan_autoremove(
        self, expected_node: str, vmid: int
    ) -> ParsedAutoremoveSimulation:
        """Return a fresh fixed autoremove simulation without metadata refresh."""
        payload = await self._async_update_request(
            expected_node,
            vmid,
            OPERATION_PLAN_AUTOREMOVE,
            timeout=TRANSPORT_TIMEOUT_SECONDS,
            timeout_outcome=PackageUpdateOutcome.PLAN_FAILED,
            connection_outcome=PackageUpdateOutcome.PLAN_FAILED,
        )
        evidence = _parse_update_response(
            payload, expected_node, vmid, OPERATION_PLAN_AUTOREMOVE
        )
        try:
            native_architecture = evidence["native_architecture"]
            installed_inventory = evidence["installed_inventory"]
            simulation = evidence["autoremove_simulation"]
        except KeyError as err:
            raise PackageUpdateError(
                PackageUpdateOutcome.PLAN_FAILED,
                "cleanup plan helper returned incomplete evidence",
            ) from err
        if not all(
            isinstance(value, str)
            for value in (native_architecture, installed_inventory, simulation)
        ):
            raise PackageUpdateError(
                PackageUpdateOutcome.PLAN_FAILED,
                "cleanup plan helper returned malformed evidence",
            )
        try:
            return parse_autoremove_simulation(
                simulation,
                native_architecture=native_architecture,
                installed_inventory=installed_inventory,
            )
        except PackageScanParseError as err:
            raise PackageUpdateError(PackageUpdateOutcome.PLAN_FAILED, str(err)) from err

    async def async_autoremove(
        self, expected_node: str, vmid: int
    ) -> PackageMutationResult:
        """Run the fixed autoremove mutation and parse inventory evidence."""
        payload = await self._async_update_request(
            expected_node,
            vmid,
            OPERATION_AUTOREMOVE_PACKAGES,
            timeout=UPDATE_TRANSPORT_TIMEOUT_SECONDS,
            timeout_outcome=PackageUpdateOutcome.MUTATION_TIMED_OUT,
            connection_outcome=PackageUpdateOutcome.MUTATION_UNCERTAIN,
        )
        evidence = _parse_update_response(
            payload, expected_node, vmid, OPERATION_AUTOREMOVE_PACKAGES
        )
        try:
            before_text = evidence["before_inventory"]
            after_text = evidence["after_inventory"]
        except KeyError as err:
            raise PackageUpdateError(
                PackageUpdateOutcome.MUTATION_UNCERTAIN,
                "autoremove helper returned incomplete inventory evidence",
            ) from err
        if not isinstance(before_text, str) or not isinstance(after_text, str):
            raise PackageUpdateError(
                PackageUpdateOutcome.MUTATION_UNCERTAIN,
                "autoremove helper returned malformed inventory evidence",
            )
        try:
            before = parse_installed_inventory(before_text)
            after = parse_installed_inventory(after_text)
        except PackageScanParseError as err:
            raise PackageUpdateError(
                PackageUpdateOutcome.MUTATION_FAILED,
                "post-autoremove package inventory could not be parsed",
            ) from err
        if before.unfinished or after.unfinished:
            raise PackageUpdateError(
                PackageUpdateOutcome.MUTATION_FAILED,
                "dpkg reports unfinished package state after autoremove",
            )
        return PackageMutationResult(before=before.installed, after=after.installed)

    async def async_ping(self, expected_node: str, vmid: int) -> bool:
        """Ask the validated running LXC to execute only ``/bin/true``."""
        payload = await self._async_update_request(
            expected_node,
            vmid,
            OPERATION_PING,
            timeout=PING_TRANSPORT_TIMEOUT_SECONDS,
            timeout_outcome=PackageUpdateOutcome.LIVENESS_FAILED,
            connection_outcome=PackageUpdateOutcome.LIVENESS_FAILED,
        )
        evidence = _parse_update_response(payload, expected_node, vmid, OPERATION_PING)
        if evidence.get("pong") is not True or set(evidence) != {"pong"}:
            raise PackageUpdateError(
                PackageUpdateOutcome.LIVENESS_FAILED,
                "package helper returned malformed liveness evidence",
            )
        return True

    async def _async_update_request(
        self,
        expected_node: str,
        vmid: int,
        operation: str,
        *,
        timeout: float,
        timeout_outcome: PackageUpdateOutcome,
        connection_outcome: PackageUpdateOutcome,
    ) -> Any:
        """Send one typed update-path request and classify transport failures."""
        _validate_update_target(expected_node, vmid)
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": operation,
            "target": {"node": expected_node, "vmid": vmid},
        }
        try:
            return await self._async_request(request, timeout=timeout)
        except PackageTransportTimeoutError as err:
            raise PackageUpdateError(timeout_outcome, "package operation timed out") from err
        except (
            PackageTransportConnectionError,
            PackageTransportAuthenticationError,
            PackageTransportHostKeyError,
            PackageHelperUnavailableError,
            PackageHelperProtocolError,
        ) as err:
            raise PackageUpdateError(connection_outcome, str(err)) from err

    async def _async_request(
        self, request: Mapping[str, Any], *, timeout: float
    ) -> Any:
        """Send one bounded typed request through the pinned SSH connection."""
        self._require_configured()
        encoded = json.dumps(request, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise PackageHelperProtocolError("package helper request exceeded its bound")
        try:
            async with asyncio.timeout(timeout):
                async with self._connector(
                    self._endpoint,
                    port=self._port,
                    username="root",
                    config=None,
                    known_hosts=self._known_hosts_data,
                    client_keys=[self._client_key],
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
            raise PackageTransportTimeoutError("package helper timed out") from err
        except asyncssh.HostKeyNotVerifiable as err:
            raise PackageTransportHostKeyError("PVE SSH host key did not match") from err
        except asyncssh.PermissionDenied as err:
            raise PackageTransportAuthenticationError(
                "PVE SSH authentication failed"
            ) from err
        except (asyncssh.Error, OSError, ValueError) as err:
            raise PackageTransportConnectionError("PVE SSH connection failed") from err

        if not isinstance(stdout, bytes):
            raise PackageHelperProtocolError("package helper returned invalid output")
        if completed.returncode != 0 and not stdout:
            _LOGGER.debug(
                "package scan helper exited %s with bounded diagnostics: %r",
                completed.returncode,
                stderr[:200],
            )
            raise PackageHelperUnavailableError(
                "helper is missing or could not be executed"
            )
        try:
            payload = json.loads(stdout.decode())
        except (UnicodeDecodeError, ValueError) as err:
            raise PackageHelperProtocolError(
                "package helper returned a malformed response"
            ) from err
        if (
            isinstance(payload, Mapping)
            and type(payload.get("protocol_version")) is int
            and payload["protocol_version"] == PROTOCOL_VERSION
            and type(payload.get("helper_version")) is int
            and payload["helper_version"] >= 1
        ):
            self._observed_helper_version = payload["helper_version"]
        # The deployed helper contract is ok:true -> exit 0; ok:false may
        # legitimately use a nonzero exit. A structured success payload
        # paired with an abnormal exit (including no observed exit code at
        # all) is contradictory completion evidence and must fail closed
        # rather than be accepted as a successful scan.
        if isinstance(payload, Mapping) and payload.get("ok") is True:
            if completed.returncode != 0:
                raise PackageHelperProtocolError(
                    "package helper reported success with an abnormal exit"
                )
        return payload


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

    ``kill()`` alone only sends the peer a remote-signal *request*; a peer
    that ignores it (or never processes signals at all) leaves the local
    channel open indefinitely, and ``wait_closed()`` after only ``kill()``
    can hang regardless of the peer's behavior. ``close()`` additionally
    closes the local send/recv channel state and sends our own channel
    close, which -- per RFC 4254 -- the peer's SSH transport layer must
    acknowledge even if the misbehaving application-level peer process
    itself never reacts; only after ``close()`` does ``wait_closed()``
    reliably resolve promptly instead of depending on peer cooperation.
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
                process.close()
                await process.wait_closed()
                raise PackageHelperProtocolError(
                    "package helper output exceeded its bound"
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


def _validate_update_target(expected_node: str, vmid: int) -> None:
    """Validate update-path identity without translating through scan errors."""
    if not _NODE_RE.fullmatch(expected_node) or type(vmid) is not int or not (
        100 <= vmid <= 999_999_999
    ):
        raise PackageUpdateError(
            PackageUpdateOutcome.GUEST_UNAVAILABLE,
            "Proxmox LXC identity is invalid for package operations",
        )


def _parse_update_response(
    payload: Any,
    expected_node: str,
    expected_vmid: int,
    expected_operation: str,
) -> Mapping[str, Any]:
    """Validate one plan/mutation response and return its evidence."""
    if not isinstance(payload, Mapping):
        raise PackageUpdateError(
            PackageUpdateOutcome.HELPER_OUTDATED,
            "package helper is incompatible; run the bootstrap command again",
        )
    if (
        type(payload.get("protocol_version")) is not int
        or payload.get("protocol_version") != PROTOCOL_VERSION
        or type(payload.get("helper_version")) is not int
        or payload.get("helper_version") < 1
        or payload.get("operation") != expected_operation
    ):
        raise PackageUpdateError(
            PackageUpdateOutcome.HELPER_OUTDATED,
            "package helper is outdated; run the current bootstrap command again",
        )

    target = payload.get("target")
    target_matches = (
        isinstance(target, Mapping)
        and bool(target)
        and target.get("node") == expected_node
        and type(target.get("vmid")) is int
        and target.get("vmid") == expected_vmid
    )
    if payload.get("ok") is True:
        if not target_matches:
            raise PackageUpdateError(
                PackageUpdateOutcome.GUEST_UNAVAILABLE,
                "package helper returned the wrong node or LXC identity",
            )
    else:
        error = payload.get("error")
        if isinstance(target, Mapping) and target and not target_matches:
            raise PackageUpdateError(
                PackageUpdateOutcome.GUEST_UNAVAILABLE,
                "package helper returned the wrong node or LXC identity",
            )
        classification = (
            error.get("classification") if isinstance(error, Mapping) else None
        )
        if classification == "package_manager_busy":
            outcome = PackageUpdateOutcome.PACKAGE_MANAGER_BUSY
        elif classification in {"guest_unavailable", "identity_mismatch"}:
            outcome = PackageUpdateOutcome.GUEST_UNAVAILABLE
        elif expected_operation in {
            OPERATION_PLAN_PACKAGES,
            OPERATION_PLAN_AUTOREMOVE,
        }:
            outcome = PackageUpdateOutcome.PLAN_FAILED
        elif expected_operation == OPERATION_PING:
            outcome = PackageUpdateOutcome.LIVENESS_FAILED
        elif classification == "timeout":
            outcome = PackageUpdateOutcome.MUTATION_TIMED_OUT
        else:
            outcome = PackageUpdateOutcome.MUTATION_FAILED
        raise PackageUpdateError(outcome, f"package operation failed: {outcome}")

    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        outcome = (
            PackageUpdateOutcome.PLAN_FAILED
            if expected_operation in {
                OPERATION_PLAN_PACKAGES,
                OPERATION_PLAN_AUTOREMOVE,
            }
            else (
                PackageUpdateOutcome.LIVENESS_FAILED
                if expected_operation == OPERATION_PING
                else PackageUpdateOutcome.MUTATION_UNCERTAIN
            )
        )
        raise PackageUpdateError(outcome, "package helper returned malformed evidence")
    return evidence


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
        or payload.get("helper_version") < 1
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
