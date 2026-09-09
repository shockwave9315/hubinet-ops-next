"""Tests for the native AsyncSSH package transport."""

import asyncio
from contextlib import asynccontextmanager
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import asyncssh
import pytest

from custom_components.hubinet_ops.packages.models import (
    PackageScanError,
    PackageScanFailure,
)
from custom_components.hubinet_ops.packages.transport import (
    AsyncSSHPackageTransport,
    PackageHelperProtocolError,
    PackageHelperUnavailableError,
    PackageTransportAuthenticationError,
    PackageTransportConnectionError,
    PackageTransportHostKeyError,
    PROBE_TIMEOUT_SECONDS,
    TRANSPORT_TIMEOUT_SECONDS,
)
from homeassistant.core import HomeAssistant

ZERO_SIMULATION = "0 upgraded, 0 newly installed, 0 to remove and 7 not upgraded.\n"


def _response(
    *,
    node: str = "pve1",
    vmid: int = 200,
    protocol_version: int = 1,
    helper_version: int = 1,
    operation: str = "scan_packages",
    reboot_required: bool | None = False,
) -> dict[str, object]:
    return {
        "protocol_version": protocol_version,
        "helper_version": helper_version,
        "operation": operation,
        "target": {"node": node, "vmid": vmid},
        "ok": True,
        "evidence": {
            "os_release": 'ID=debian\nVERSION_ID="12"\n',
            "native_architecture": "amd64\n",
            "installed_inventory": "",
            "simulation": ZERO_SIMULATION,
            "reboot_required": reboot_required,
        },
    }


class FakeReader:
    """AsyncSSH stream double that only returns partial chunks per call.

    Real ``SSHReader.read(n)`` returns as soon as any data is available; it
    does not wait to fill ``n`` bytes or reach EOF. ``chunk_size`` lets a
    test force delivery in small pieces to prove multi-packet responses are
    still fully collected.
    """

    def __init__(self, value: bytes, *, chunk_size: int | None = None) -> None:
        """Initialize the stream contents and optional forced chunk size."""
        self._value = value
        self._offset = 0
        self._chunk_size = chunk_size

    async def read(self, size: int = -1) -> bytes:
        """Return a bounded slice of the remaining buffered content."""
        if self._offset >= len(self._value):
            return b""
        limit = size if size >= 0 else len(self._value)
        if self._chunk_size is not None:
            limit = min(limit, self._chunk_size)
        chunk = self._value[self._offset : self._offset + limit]
        self._offset += len(chunk)
        return chunk


class BlockingReader:
    """A stream double that never completes a read on its own.

    Used to prove that terminating on one stream's oversize bound does not
    wait for its peer to ever reach EOF -- the peer's still-pending read
    must be cancelled promptly instead of left to hang.
    """

    def __init__(self) -> None:
        """Initialize with no read ever having completed or been cancelled."""
        self.cancelled = False

    async def read(self, size: int = -1) -> bytes:
        """Block until cancelled, like data that will never arrive."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return b""  # pragma: no cover - unreachable, event is never set


class FakeProcess:
    """AsyncSSH process double returning one structured response."""

    def __init__(
        self,
        payload: dict[str, object],
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        stdout_chunk_size: int | None = None,
        stdout_reader: object | None = None,
        stderr_reader: object | None = None,
    ) -> None:
        """Initialize encoded output, separate stderr, and an exit status."""
        self.returncode = returncode
        self.stdout = stdout_reader or FakeReader(
            json.dumps(payload).encode(), chunk_size=stdout_chunk_size
        )
        self.stderr = stderr_reader or FakeReader(stderr)
        self.killed = False
        self.closed = False

    async def wait(self, check: bool = False):
        """Return completion evidence without interpreting the exit code."""
        return self

    def kill(self) -> None:
        """Record a best-effort remote-signal kill request."""
        self.killed = True

    def close(self) -> None:
        """Record local channel closure, independent of the kill signal."""
        self.closed = True

    async def wait_closed(self) -> None:
        """Complete termination immediately."""


class FakeConnection:
    """AsyncSSH connection double capturing the session shape."""

    def __init__(self, process: FakeProcess) -> None:
        """Initialize the connection with its one process result."""
        self.process = process
        self.process_args = None
        self.process_kwargs = None

    async def create_process(self, *args, **kwargs):
        """Capture a shell-less forced-command session request."""
        self.process_args = args
        self.process_kwargs = kwargs
        return self.process


class FakeConnector:
    """AsyncSSH connector double capturing endpoint and trust options."""

    def __init__(self, connection: FakeConnection) -> None:
        """Initialize the connector with its one connection."""
        self.connection = connection
        self.args = None
        self.kwargs = None

    def __call__(self, *args, **kwargs):
        """Return an async context manager for the fake connection."""
        self.args = args
        self.kwargs = kwargs

        @asynccontextmanager
        async def context():
            yield self.connection

        return context()


class FailingConnector:
    """Connection factory which fails while entering the SSH context."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __call__(self, *args, **kwargs):
        @asynccontextmanager
        async def context():
            raise self.error
            yield  # pragma: no cover

        return context()


async def _transport(
    hass: HomeAssistant, tmp_path: Path, payload: dict[str, object], **process_kwargs
):
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key_text = host_key.export_public_key().decode().strip()
    connection = FakeConnection(FakeProcess(payload, **process_kwargs))
    connector = FakeConnector(connection)
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10",
        private_key=private_key.export_private_key().decode(),
        host_key=host_key_text,
        connector=connector,
    )
    await transport.async_prepare(hass)
    return transport, connector, connection, private_key, host_key_text


async def test_asyncssh_transport_separates_endpoint_and_identity_and_pins_auth(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """SSH uses CONF_HOST while the helper request carries node/VMID identity."""
    transport, connector, connection, private_key, host_key = await _transport(
        hass, tmp_path, _response()
    )
    result = await transport.async_scan("pve1", 200)
    assert result.packages == ()
    assert result.not_upgraded_count == 7
    assert result.reboot_required is False

    assert connector.args == ("192.0.2.10",)
    assert connector.args[0] != "pve1"
    assert connector.kwargs["port"] == 22
    assert connector.kwargs["known_hosts"] == f"192.0.2.10 {host_key}\n".encode()
    assert connector.kwargs["client_keys"][0].get_fingerprint() == (
        private_key.get_fingerprint()
    )
    assert connector.kwargs["config"] is None
    assert connector.kwargs["agent_path"] is None
    assert connector.kwargs["pkcs11_provider"] is None
    assert connector.kwargs["password"] is None
    assert connector.kwargs["password_auth"] is False
    assert connector.kwargs["kbdint_auth"] is False
    assert connector.kwargs["host_based_auth"] is False
    assert connector.kwargs["gss_auth"] is False
    assert connector.kwargs["preferred_auth"] == ("publickey",)
    assert connector.kwargs["disable_trivial_auth"] is True

    assert connection.process_args == ()
    request = json.loads(connection.process_kwargs["input"])
    assert request == {
        "protocol_version": 1,
        "operation": "scan_packages",
        "target": {"node": "pve1", "vmid": 200},
    }
    assert connection.process_kwargs["request_pty"] is False
    # B1 regression: create_process must never be called with a kwarg that
    # only belongs at the connection level, such as agent_forwarding, since
    # create_process forwards unknown kwargs to create_session which has no
    # **kwargs sink and rejects them with a real TypeError.
    assert "agent_forwarding" not in connection.process_kwargs
    # N3 regression: stderr must never be merged into the JSON stdout
    # stream.
    assert "stderr" not in connection.process_kwargs


def test_create_process_kwargs_bind_against_real_asyncssh_session() -> None:
    """B1: the transport's create_process kwargs bind against real AsyncSSH.

    ``create_process`` only consumes ``input``/``stdin``/``stdout``/
    ``stderr``/``bufsize``/``send_eof``/``recv_eof`` itself; every other
    kwarg is forwarded verbatim to ``create_session``, which declares no
    ``**kwargs`` sink. A fake ``create_process(*a, **kw)`` double that
    accepts anything (as used above) cannot catch an unsupported kwarg by
    itself; binding against the real signature can.
    """
    forwarded_kwargs = {"encoding": None, "request_pty": False}
    signature = inspect.signature(
        asyncssh.connection.SSHClientConnection.create_session
    )
    # `self` and `session_factory` stand in for the bound connection and the
    # process class create_process supplies internally.
    signature.bind(object(), object(), **forwarded_kwargs)

    # The historical bug: agent_forwarding does not bind here even though it
    # is a valid *connection*-level option (SSHClientConnectionOptions).
    with pytest.raises(TypeError):
        signature.bind(object(), object(), **forwarded_kwargs, agent_forwarding=False)


async def test_real_asyncssh_round_trip_with_chunked_stdout_and_stderr(
    hass: HomeAssistant, tmp_path: Path, socket_enabled: None
) -> None:
    """Real AsyncSSH: connect, auth, chunked stdout, and separate stderr.

    This exercises the real ``asyncssh.connect``/``create_process``/session
    machinery end to end against a local in-process server, which is what
    caught B1 (an invalid create_process kwarg) and would catch a B2
    regression (reading only one chunk instead of draining to EOF).
    """
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")

    payload = json.dumps(_response()).encode()
    received_requests: list[bytes] = []

    async def _serve_forced_command(process: asyncssh.SSHServerProcess) -> None:
        """Ignore whatever the client requested; always run the one op.

        This mirrors the forced-command boundary: the server-side handler,
        not the client, decides what runs.
        """
        request = bytearray()
        while True:
            chunk = await process.stdin.read(64)
            if not chunk:
                break
            request.extend(chunk)
        received_requests.append(bytes(request))
        # Force delivery across many small SSH data packets/reads instead
        # of one shot, so a single `read()` call could not collect it all.
        for offset in range(0, len(payload), 5):
            process.stdout.write(payload[offset : offset + 5])
            await process.stdout.drain()
        # A warning on stderr, interleaved with the JSON on stdout, must
        # never corrupt the parsed response (N3).
        process.stderr.write(b"apt-get: warning: harmless diagnostic\n")
        await process.stderr.drain()
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=asyncssh.import_authorized_keys(
            client_key.export_public_key().decode()
        ),
        process_factory=_serve_forced_command,
        encoding=None,
    )
    try:
        port = server.sockets[0].getsockname()[1]
        transport = AsyncSSHPackageTransport(
            endpoint="127.0.0.1",
            private_key=client_key.export_private_key().decode(),
            host_key=host_key.export_public_key().decode().strip(),
            port=port,
        )
        await transport.async_prepare(hass)
        assert transport.configured is True

        result = await transport.async_scan("pve1", 200)
    finally:
        server.close()
        await server.wait_closed()

    assert result.packages == ()
    assert result.not_upgraded_count == 7
    assert json.loads(received_requests[0]) == {
        "protocol_version": 1,
        "operation": "scan_packages",
        "target": {"node": "pve1", "vmid": 200},
    }


async def test_real_asyncssh_success_payload_with_nonzero_exit_fails_closed(
    hass: HomeAssistant, tmp_path: Path, socket_enabled: None
) -> None:
    """C4: a real peer's valid ok:true JSON with a nonzero exit fails closed.

    The deployed helper contract is ok:true -> exit 0. A real server that
    writes a structurally valid success payload but then exits nonzero
    delivers contradictory completion evidence, which must not be
    accepted as a successful scan.
    """
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")

    payload = json.dumps(_response()).encode()

    async def _serve_success_but_nonzero_exit(
        process: asyncssh.SSHServerProcess,
    ) -> None:
        while True:
            chunk = await process.stdin.read(64)
            if not chunk:
                break
        process.stdout.write(payload)
        await process.stdout.drain()
        process.exit(1)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=asyncssh.import_authorized_keys(
            client_key.export_public_key().decode()
        ),
        process_factory=_serve_success_but_nonzero_exit,
        encoding=None,
    )
    try:
        port = server.sockets[0].getsockname()[1]
        transport = AsyncSSHPackageTransport(
            endpoint="127.0.0.1",
            private_key=client_key.export_private_key().decode(),
            host_key=host_key.export_public_key().decode().strip(),
            port=port,
        )
        await transport.async_prepare(hass)

        with pytest.raises(PackageScanError) as caught:
            await transport.async_scan("pve1", 200)
    finally:
        server.close()
        await server.wait_closed()

    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED


async def test_real_asyncssh_stuck_peer_oversize_terminates_promptly(
    hass: HomeAssistant, tmp_path: Path, socket_enabled: None
) -> None:
    """N1: oversize termination must not depend on the peer honoring kill().

    ``kill()`` only sends the peer a remote-signal *request*. A real server
    handler that ignores it, never exits, and never reacts is used here so
    ``process.close()`` -- not the peer -- is what makes local channel
    closure (and therefore ``wait_closed()``) resolve promptly. Without it
    this scenario previously hung until the 300s outer transport timeout.
    """
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")

    server_tasks: list[asyncio.Task] = []

    async def _stuck_forced_command(process: asyncssh.SSHServerProcess) -> None:
        """Write past the bound, then hang forever, ignoring any signal."""
        server_tasks.append(asyncio.current_task())
        process.stdout.write(b"x" * 64)
        await process.stdout.drain()
        # Deliberately never exits, never reacts to a kill/terminate
        # request, and never closes its own side of the session.
        await asyncio.sleep(3600)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=asyncssh.import_authorized_keys(
            client_key.export_public_key().decode()
        ),
        process_factory=_stuck_forced_command,
        encoding=None,
    )
    try:
        port = server.sockets[0].getsockname()[1]
        transport = AsyncSSHPackageTransport(
            endpoint="127.0.0.1",
            private_key=client_key.export_private_key().decode(),
            host_key=host_key.export_public_key().decode().strip(),
            port=port,
        )
        await transport.async_prepare(hass)

        tasks_before = asyncio.all_tasks()
        with (
            patch(
                "custom_components.hubinet_ops.packages.transport._MAX_RESPONSE_BYTES",
                4,
            ),
            pytest.raises(PackageScanError) as caught,
        ):
            # Bounded far under the 300s outer transport timeout: a hang
            # here means local closure is still depending on peer
            # cooperation.
            await asyncio.wait_for(transport.async_scan("pve1", 200), timeout=10)
    finally:
        server.close()
        await server.wait_closed()
        # The server-side handler is the one deliberately never exiting
        # here; clean it up explicitly since closing the server does not
        # cancel an in-flight process_factory task on its own.
        for task in server_tasks:
            task.cancel()
        if server_tasks:
            await asyncio.gather(*server_tasks, return_exceptions=True)

    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED
    await asyncio.sleep(0)
    leaked_pending = [
        task
        for task in asyncio.all_tasks() - tasks_before - {asyncio.current_task()}
        if not task.done()
    ]
    assert leaked_pending == []


@pytest.mark.parametrize(
    ("private_key", "host_key"), [(None, None), ("private", None), (None, "host")]
)
async def test_transport_requires_both_in_memory_trust_inputs(
    private_key: str | None, host_key: str | None
) -> None:
    """Missing either trust input disables and blocks the transport."""
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10", private_key=private_key, host_key=host_key
    )
    assert transport.configured is False
    with pytest.raises(Exception, match="trust material is not configured"):
        await transport.async_probe()


def test_transport_configured_from_valid_in_memory_keys() -> None:
    """Valid Ed25519 identities make the transport ready immediately."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10",
        private_key=private_key.export_private_key().decode(),
        host_key=host_key.export_public_key().decode().strip(),
    )
    assert transport.configured is True


@pytest.mark.parametrize(
    "response",
    [
        _response(protocol_version=2),
        _response(helper_version=0),
        _response(operation="other"),
        {**_response(), "protocol_version": True},
        {**_response(), "protocol_version": 1.0},
    ],
)
async def test_incompatible_helper_protocol_fails_clearly(
    hass: HomeAssistant, tmp_path: Path, response: dict[str, object]
) -> None:
    """No negotiation is attempted for incompatible helper metadata."""
    transport, *_ = await _transport(hass, tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.PROTOCOL_MISMATCH


async def test_scan_accepts_newer_informational_helper_version(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Protocol version, not helper implementation version, is authoritative."""
    transport, *_ = await _transport(hass, tmp_path, _response(helper_version=99))
    assert (await transport.async_scan("pve1", 200)).packages == ()


async def test_probe_returns_local_node_without_target(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Probe uses its minimal exact request and returns typed node identity."""
    response = {
        "protocol_version": 1,
        "helper_version": 2,
        "operation": "probe",
        "ok": True,
        "node": "pve1",
    }
    transport, _connector, connection, *_ = await _transport(
        hass, tmp_path, response
    )
    probe = await transport.async_probe()
    assert probe.node == "pve1"
    assert probe.helper_version == 2
    assert json.loads(connection.process_kwargs["input"]) == {
        "protocol_version": 1,
        "operation": "probe",
    }


async def test_probe_and_scan_use_distinct_bounded_timeouts(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Setup probing is short while potentially long package scans stay unchanged."""
    probe_response = {
        "protocol_version": 1,
        "helper_version": 2,
        "operation": "probe",
        "ok": True,
        "node": "pve1",
    }
    transport, *_ = await _transport(hass, tmp_path, probe_response)
    real_timeout = asyncio.timeout
    with patch(
        "custom_components.hubinet_ops.packages.transport.asyncio.timeout",
        side_effect=real_timeout,
    ) as timeout:
        await transport.async_probe()
    timeout.assert_called_once_with(PROBE_TIMEOUT_SECONDS)
    assert PROBE_TIMEOUT_SECONDS == 30.0

    transport, *_ = await _transport(hass, tmp_path, _response())
    with patch(
        "custom_components.hubinet_ops.packages.transport.asyncio.timeout",
        side_effect=real_timeout,
    ) as timeout:
        await transport.async_scan("pve1", 200)
    timeout.assert_called_once_with(TRANSPORT_TIMEOUT_SECONDS)
    assert TRANSPORT_TIMEOUT_SECONDS == 300.0


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        ([], PackageHelperProtocolError),
        (
            {
                "protocol_version": 2,
                "helper_version": 2,
                "operation": "probe",
                "ok": True,
                "node": "pve1",
            },
            PackageHelperProtocolError,
        ),
        (_response(helper_version=1), PackageHelperUnavailableError),
    ],
)
async def test_probe_rejects_malformed_wrong_protocol_and_old_helper(
    hass: HomeAssistant,
    tmp_path: Path,
    response: object,
    error_type: type[Exception],
) -> None:
    """Probe failures distinguish incompatible data from an old helper."""
    transport, *_ = await _transport(hass, tmp_path, response)
    with pytest.raises(error_type):
        await transport.async_probe()


@pytest.mark.parametrize(
    ("ssh_error", "transport_error"),
    [
        (
            asyncssh.HostKeyNotVerifiable("host key mismatch"),
            PackageTransportHostKeyError,
        ),
        (asyncssh.PermissionDenied("denied"), PackageTransportAuthenticationError),
        (OSError("refused"), PackageTransportConnectionError),
    ],
)
async def test_probe_classifies_connection_failures(
    ssh_error: Exception, transport_error: type[Exception]
) -> None:
    """Host-key mismatch, auth rejection, and connectivity stay distinct."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10",
        private_key=private_key.export_private_key().decode(),
        host_key=host_key.export_public_key().decode().strip(),
        connector=FailingConnector(ssh_error),
    )
    with pytest.raises(transport_error):
        await transport.async_probe()


async def test_scan_preserves_timeout_classification() -> None:
    """The shared probe transport does not erase the existing scan timeout result."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10",
        private_key=private_key.export_private_key().decode(),
        host_key=host_key.export_public_key().decode().strip(),
        connector=FailingConnector(TimeoutError()),
    )
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.TIMEOUT


@pytest.mark.parametrize("response", [_response(node="pve2"), _response(vmid=201)])
async def test_helper_identity_mismatch_fails_closed(
    hass: HomeAssistant, tmp_path: Path, response: dict[str, object]
) -> None:
    """A response for another node or VMID never becomes package evidence."""
    transport, *_ = await _transport(hass, tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.IDENTITY_MISMATCH


@pytest.mark.parametrize("returncode", [1, 2, -9, None])
async def test_success_payload_with_nonzero_exit_fails_closed(
    hass: HomeAssistant, tmp_path: Path, returncode: int | None
) -> None:
    """C4-A: ok:true paired with a nonzero (or missing) exit fails closed.

    Reproduces the exact matrix an empirical audit found accepted as
    SUCCESS: valid ok:true JSON with rc 1, rc 2, rc -9, or no observed
    exit code at all. The deployed helper contract is ok:true -> exit 0;
    anything else is contradictory completion evidence.
    """
    transport, *_ = await _transport(
        hass, tmp_path, _response(), returncode=returncode
    )
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED


async def test_ok_false_response_with_nonzero_exit_preserves_its_classification(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """C4-B: ok:false failures are unaffected by the ok:true/exit check.

    The helper's failure contract may legitimately use a nonzero exit; the
    C4 fix must stay narrow to a *success* payload paired with an abnormal
    exit, not broaden into rejecting every nonzero exit and losing the
    original semantic classification.
    """
    response = {
        "protocol_version": 1,
        "helper_version": 1,
        "operation": "scan_packages",
        "target": {"node": "pve1", "vmid": 200},
        "ok": False,
        "error": {
            "classification": "guest_unavailable",
            "message": "LXC is not running",
        },
    }
    transport, *_ = await _transport(hass, tmp_path, response, returncode=1)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.GUEST_UNAVAILABLE


async def test_helper_pre_target_failure_surfaces_its_real_classification(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """N8: a failure raised before any target was validated is not relabeled.

    The forced-command helper rejects some requests (wrong caller identity,
    unparsable input) before it has a target to validate at all, and
    responds with an empty target. That must not be misclassified as
    IDENTITY_MISMATCH.
    """
    response = {
        "protocol_version": 1,
        "helper_version": 1,
        "operation": "scan_packages",
        "target": {},
        "ok": False,
        "error": {
            "classification": "execution_failed",
            "message": "package scan helper must run as root",
        },
    }
    transport, *_ = await _transport(hass, tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED
    assert "must run as root" in str(caught.value)


async def test_helper_failure_with_validated_target_still_checks_identity(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """N8: a failure that does carry a target must still match identity."""
    response = {
        "protocol_version": 1,
        "helper_version": 1,
        "operation": "scan_packages",
        "target": {"node": "pve1", "vmid": 999},
        "ok": False,
        "error": {"classification": "guest_unavailable", "message": "nope"},
    }
    transport, *_ = await _transport(hass, tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.IDENTITY_MISMATCH


async def test_transport_preserves_semantic_parser_failure(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Unfinished dpkg evidence reaches state as DPKG_UNFINISHED."""
    response = _response()
    response["evidence"]["installed_inventory"] = "foo\tamd64\t1.0\thalf-configured\n"
    transport, *_ = await _transport(hass, tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.DPKG_UNFINISHED


async def test_transport_reads_response_split_across_many_small_chunks(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """B2: a response delivered in small pieces still fully parses.

    ``SSHReader.read(n)`` returns as soon as any data is available; it does
    not fill ``n`` bytes or wait for EOF. A transport that read only once
    would truncate this response and fail to parse it.
    """
    transport, *_ = await _transport(
        hass, tmp_path, _response(), stdout_chunk_size=3
    )
    result = await transport.async_scan("pve1", 200)
    assert result.not_upgraded_count == 7


async def test_transport_rejects_oversized_combined_output(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Transport output is bounded even when the forced-command peer misbehaves."""
    transport, _connector, connection, *_ = await _transport(
        hass, tmp_path, _response()
    )
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport._MAX_RESPONSE_BYTES", 4
        ),
        pytest.raises(PackageScanError) as caught,
    ):
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED
    assert connection.process.killed is True
    assert connection.process.closed is True


async def test_oversized_stdout_terminates_promptly_even_if_stderr_never_ends(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Oversize stdout must not wait on a stderr stream that never reaches EOF.

    stdout and stderr share the same SSH channel receive window; once
    stdout is abandoned after exceeding its bound, a still-active stderr
    reader can stall on window-blocked data that will never arrive. This
    must be caught by killing the process as soon as EITHER bound is
    exceeded, not by waiting for the 300-second outer transport timeout.
    """
    stderr_reader = BlockingReader()
    transport, _connector, connection, *_ = await _transport(
        hass, tmp_path, _response(), stderr_reader=stderr_reader
    )
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport._MAX_RESPONSE_BYTES", 4
        ),
        pytest.raises(PackageScanError) as caught,
    ):
        await asyncio.wait_for(transport.async_scan("pve1", 200), timeout=5)
    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED
    assert connection.process.killed is True
    assert connection.process.closed is True
    assert stderr_reader.cancelled is True


async def test_oversized_stderr_terminates_promptly_even_if_stdout_never_ends(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Oversize stderr must not wait on a stdout stream that never reaches EOF.

    Symmetric to the stdout case: an oversize diagnostic stream must not be
    able to stall bounded termination behind a stdout peer that never
    completes.
    """
    stdout_reader = BlockingReader()
    transport, _connector, connection, *_ = await _transport(
        hass,
        tmp_path,
        _response(),
        stdout_reader=stdout_reader,
        stderr=b"x" * 100,
    )
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport._MAX_STDERR_BYTES", 4
        ),
        pytest.raises(PackageScanError) as caught,
    ):
        await asyncio.wait_for(transport.async_scan("pve1", 200), timeout=5)
    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED
    assert connection.process.killed is True
    assert connection.process.closed is True
    assert stdout_reader.cancelled is True


async def test_stderr_diagnostics_never_corrupt_the_json_response(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """N3: stderr content, however large, is parsed separately from stdout."""
    transport, *_ = await _transport(
        hass,
        tmp_path,
        _response(),
        stderr=b"warning: something noisy\n" * 100,
    )
    result = await transport.async_scan("pve1", 200)
    assert result.not_upgraded_count == 7
