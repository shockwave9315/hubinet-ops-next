"""Tests for the native AsyncSSH package transport."""

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
from custom_components.hubinet_ops.packages.transport import AsyncSSHPackageTransport
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


class FakeProcess:
    """AsyncSSH process double returning one structured response."""

    def __init__(
        self,
        payload: dict[str, object],
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        stdout_chunk_size: int | None = None,
    ) -> None:
        """Initialize encoded output, separate stderr, and an exit status."""
        self.returncode = returncode
        self.stdout = FakeReader(
            json.dumps(payload).encode(), chunk_size=stdout_chunk_size
        )
        self.stderr = FakeReader(stderr)
        self.killed = False

    async def wait(self, check: bool = False):
        """Return completion evidence without interpreting the exit code."""
        return self

    def kill(self) -> None:
        """Record bounded-output termination."""
        self.killed = True

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


async def _transport(
    hass: HomeAssistant, tmp_path: Path, payload: dict[str, object], **process_kwargs
):
    private_key = tmp_path / "hubinet_ops"
    known_hosts = tmp_path / "known_hosts"
    private_key.write_text("private key")
    known_hosts.write_text("host key")
    connection = FakeConnection(FakeProcess(payload, **process_kwargs))
    connector = FakeConnector(connection)
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10",
        private_key_path=private_key,
        known_hosts_path=known_hosts,
        connector=connector,
    )
    await transport.async_prepare(hass)
    return transport, connector, connection, private_key, known_hosts


async def test_asyncssh_transport_separates_endpoint_and_identity_and_pins_auth(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """SSH uses CONF_HOST while the helper request carries node/VMID identity."""
    transport, connector, connection, private_key, known_hosts = await _transport(
        hass, tmp_path, _response()
    )
    result = await transport.async_scan("pve1", 200)
    assert result.packages == ()
    assert result.not_upgraded_count == 7
    assert result.reboot_required is False

    assert connector.args == ("192.0.2.10",)
    assert connector.args[0] != "pve1"
    assert connector.kwargs["port"] == 22
    assert connector.kwargs["known_hosts"] == known_hosts.read_bytes()
    assert connector.kwargs["client_keys"] == [str(private_key)]
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

    private_key_path = tmp_path / "hubinet_ops"
    client_key.write_private_key(private_key_path)
    private_key_path.chmod(0o600)

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
        known_hosts_path = tmp_path / "known_hosts"
        known_hosts_path.write_bytes(
            f"[127.0.0.1]:{port} ".encode() + host_key.export_public_key()
        )

        transport = AsyncSSHPackageTransport(
            endpoint="127.0.0.1",
            private_key_path=private_key_path,
            known_hosts_path=known_hosts_path,
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


def test_transport_prerequisites_require_both_nonempty_trust_files(
    tmp_path: Path,
) -> None:
    """Missing or empty private-key/known-hosts material disables transport."""
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10",
        private_key_path=tmp_path / "key",
        known_hosts_path=tmp_path / "known_hosts",
    )
    assert transport.configured is False
    assert transport._load_known_hosts() is None  # noqa: SLF001
    (tmp_path / "key").write_text("key")
    assert transport._load_known_hosts() is None  # noqa: SLF001
    (tmp_path / "known_hosts").write_text("")
    assert transport._load_known_hosts() is None  # noqa: SLF001
    (tmp_path / "known_hosts").write_text("host key")
    assert transport._load_known_hosts() == b"host key"  # noqa: SLF001


async def test_transport_configured_reflects_last_prepare_only(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """H2/N5: readiness is fixed at async_prepare and never re-checked live.

    Trust files changing after setup must not change `.configured` until
    the next config-entry setup (reload) calls async_prepare again.
    """
    private_key = tmp_path / "key"
    known_hosts = tmp_path / "known_hosts"
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10", private_key_path=private_key, known_hosts_path=known_hosts
    )
    assert transport.configured is False

    private_key.write_text("key")
    known_hosts.write_text("host key")
    await transport.async_prepare(hass)
    assert transport.configured is True

    known_hosts.unlink()
    assert transport.configured is True  # stale on purpose until reload

    with patch.object(Path, "is_file", side_effect=AssertionError("touched fs")):
        assert transport.configured is True


@pytest.mark.parametrize(
    "response",
    [
        _response(protocol_version=2),
        _response(helper_version=2),
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


@pytest.mark.parametrize("response", [_response(node="pve2"), _response(vmid=201)])
async def test_helper_identity_mismatch_fails_closed(
    hass: HomeAssistant, tmp_path: Path, response: dict[str, object]
) -> None:
    """A response for another node or VMID never becomes package evidence."""
    transport, *_ = await _transport(hass, tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.IDENTITY_MISMATCH


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
