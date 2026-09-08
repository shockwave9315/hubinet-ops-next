"""Tests for the native AsyncSSH package transport."""

from contextlib import asynccontextmanager
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


class FakeProcess:
    """AsyncSSH process double returning one structured response."""

    def __init__(self, payload: dict[str, object], *, returncode: int = 0) -> None:
        """Initialize encoded output and an exit status."""
        self.returncode = returncode
        self.stdout = self.FakeReader(json.dumps(payload).encode())
        self.killed = False

    class FakeReader:
        """Bound-respecting AsyncSSH stream double."""

        def __init__(self, value: bytes) -> None:
            """Initialize the stream contents."""
            self.value = value

        async def read(self, size: int = -1) -> bytes:
            """Return at most the requested amount of output."""
            return self.value if size < 0 else self.value[:size]

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


def _transport(tmp_path: Path, payload: dict[str, object]):
    private_key = tmp_path / "hubinet_ops"
    known_hosts = tmp_path / "known_hosts"
    private_key.write_text("private key")
    known_hosts.write_text("host key")
    connection = FakeConnection(FakeProcess(payload))
    connector = FakeConnector(connection)
    transport = AsyncSSHPackageTransport(
        endpoint="192.0.2.10",
        private_key_path=private_key,
        known_hosts_path=known_hosts,
        connector=connector,
    )
    return transport, connector, connection, private_key, known_hosts


async def test_asyncssh_transport_separates_endpoint_and_identity_and_pins_auth(
    tmp_path: Path,
) -> None:
    """SSH uses CONF_HOST while the helper request carries node/VMID identity."""
    transport, connector, connection, private_key, known_hosts = _transport(
        tmp_path, _response()
    )
    result = await transport.async_scan("pve1", 200)
    assert result.packages == ()
    assert result.not_upgraded_count == 7
    assert result.reboot_required is False

    assert connector.args == ("192.0.2.10",)
    assert connector.args[0] != "pve1"
    assert connector.kwargs["known_hosts"] == str(known_hosts)
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
    assert connection.process_kwargs["agent_forwarding"] is False
    assert connection.process_kwargs["stderr"] == asyncssh.STDOUT


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
    (tmp_path / "key").write_text("key")
    assert transport.configured is False
    (tmp_path / "known_hosts").write_text("")
    assert transport.configured is False
    (tmp_path / "known_hosts").write_text("host key")
    assert transport.configured is True


@pytest.mark.parametrize(
    "response",
    [
        _response(protocol_version=2),
        _response(helper_version=2),
        _response(operation="other"),
    ],
)
async def test_incompatible_helper_protocol_fails_clearly(
    tmp_path: Path, response: dict[str, object]
) -> None:
    """No negotiation is attempted for incompatible helper metadata."""
    transport, *_ = _transport(tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.PROTOCOL_MISMATCH


@pytest.mark.parametrize("response", [_response(node="pve2"), _response(vmid=201)])
async def test_helper_identity_mismatch_fails_closed(
    tmp_path: Path, response: dict[str, object]
) -> None:
    """A response for another node or VMID never becomes package evidence."""
    transport, *_ = _transport(tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.IDENTITY_MISMATCH


async def test_transport_preserves_semantic_parser_failure(tmp_path: Path) -> None:
    """Unfinished dpkg evidence reaches state as DPKG_UNFINISHED."""
    response = _response()
    response["evidence"]["installed_inventory"] = "foo\tamd64\t1.0\thalf-configured\n"
    transport, *_ = _transport(tmp_path, response)
    with pytest.raises(PackageScanError) as caught:
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.DPKG_UNFINISHED


async def test_transport_rejects_oversized_combined_output(tmp_path: Path) -> None:
    """Transport output is bounded even when the forced-command peer misbehaves."""
    transport, _connector, connection, *_ = _transport(tmp_path, _response())
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport._MAX_RESPONSE_BYTES", 4
        ),
        pytest.raises(PackageScanError) as caught,
    ):
        await transport.async_scan("pve1", 200)
    assert caught.value.failure is PackageScanFailure.EXECUTION_FAILED
    assert connection.process.killed is True
