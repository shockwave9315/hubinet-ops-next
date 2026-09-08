"""Tests for the separately deployed forced-command package helper."""

from contextlib import nullcontext
import importlib.util
from pathlib import Path
import stat
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hubinet_ops.packages.transport import TRANSPORT_TIMEOUT_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = REPO_ROOT / "deploy/hubinet-package-scan-helper.py"

TWO_UPDATES = """\
Inst openssl [3.0.11-1] (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Inst apt [2.6.1] (2.6.2 Debian:12/oldstable [amd64])
2 upgraded, 0 newly installed, 0 to remove and 41 not upgraded.
"""
TWO_INVENTORY = "openssl\tamd64\t3.0.11-1\tinstalled\napt\tamd64\t2.6.1\tinstalled\n"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("package_scan_helper", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def test_transport_deadline_exceeds_helper_operation_deadline() -> None:
    """The helper can normally classify timeout before SSH transport expires."""
    assert TRANSPORT_TIMEOUT_SECONDS > helper.OPERATION_TIMEOUT_SECONDS


class FakeHelperRunner:
    """Return deterministic evidence for every fixed helper command."""

    def __init__(
        self,
        *,
        local_node: str = "pve1",
        config_returncode: int = 0,
        status: str = "running",
        update_returncode: int = 0,
        update_stderr: str = "",
        reboot_returncode: int = 1,
    ) -> None:
        """Initialize fixed command outcomes."""
        self.local_node = local_node
        self.config_returncode = config_returncode
        self.status = status
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.reboot_returncode = reboot_returncode
        self.calls: list[tuple[tuple[str, ...], float, int]] = []

    def __call__(self, argv, timeout, max_output):
        """Return evidence for one recognized argv shape."""
        self.calls.append((argv, timeout, max_output))
        rendered = " ".join(argv)
        if argv == ("hostname",):
            return helper.CommandResult(0, f"{self.local_node}\n".encode(), b"")
        if argv[:2] == ("pct", "config"):
            return helper.CommandResult(self.config_returncode, b"arch: amd64\n", b"")
        if argv[:2] == ("pct", "status"):
            return helper.CommandResult(0, f"status: {self.status}\n".encode(), b"")
        if "/etc/os-release" in rendered:
            return helper.CommandResult(0, b'ID=debian\nVERSION_ID="12"\n', b"")
        if "apt-get --version" in rendered:
            return helper.CommandResult(0, b"apt 2.6.1 (amd64)\n", b"")
        if "apt-get update" in rendered:
            return helper.CommandResult(
                self.update_returncode, b"", self.update_stderr.encode()
            )
        if "apt-get -s upgrade" in rendered:
            return helper.CommandResult(0, TWO_UPDATES.encode(), b"")
        if "dpkg --print-architecture" in rendered:
            return helper.CommandResult(0, b"amd64\n", b"")
        if "dpkg-query" in rendered:
            return helper.CommandResult(0, TWO_INVENTORY.encode(), b"")
        if "/var/run/reboot-required" in rendered:
            return helper.CommandResult(self.reboot_returncode, b"", b"")
        raise AssertionError(f"unexpected command: {argv!r}")


def _request(vmid: int = 200, node: str = "pve1") -> dict[str, object]:
    return {
        "protocol_version": 1,
        "operation": "scan_packages",
        "target": {"node": node, "vmid": vmid},
    }


def _handle(runner: FakeHelperRunner, **kwargs):
    return helper.handle_request(
        _request(), runner=runner, lock_factory=lambda _vmid: nullcontext(), **kwargs
    )


def test_helper_uses_fixed_commands_and_returns_versioned_identity() -> None:
    """The helper accepts one operation and returns verified node/VMID metadata."""
    runner = FakeHelperRunner(reboot_returncode=0)
    response = _handle(runner)
    assert response["ok"] is True
    assert response["protocol_version"] == 1
    assert response["helper_version"] == 1
    assert response["operation"] == "scan_packages"
    assert response["target"] == {"node": "pve1", "vmid": 200}
    assert response["evidence"]["reboot_required"] is True

    commands = [call[0] for call in runner.calls]
    assert commands[:3] == [
        ("hostname",),
        ("pct", "config", "200"),
        ("pct", "status", "200"),
    ]
    guest_commands = commands[3:]
    assert all(
        command[:4] == ("pct", "exec", "200", "--") for command in guest_commands
    )
    assert any(
        command[-4:] == ("apt-get", "update", "-qq", "--error-on=any")
        for command in guest_commands
    )
    assert any(
        command[-3:] == ("apt-get", "-s", "upgrade") for command in guest_commands
    )


def test_helper_classifies_apt_busy_and_stops() -> None:
    """A busy guest dpkg lock prevents simulation with a bounded result."""
    runner = FakeHelperRunner(
        update_returncode=100,
        update_stderr="E: Could not get lock /var/lib/dpkg/lock-frontend",
    )
    response = _handle(runner)
    assert response["error"]["classification"] == "package_manager_busy"
    assert not any("-s" in call[0] for call in runner.calls)


def test_host_flock_contention_is_a_semantic_busy_result() -> None:
    """Non-blocking per-VMID flock contention is reported without commands."""
    file_stat = MagicMock(st_mode=stat.S_IFREG | 0o600)
    runner = FakeHelperRunner()
    with (
        patch.object(helper.os, "open", return_value=42),
        patch.object(helper.os, "fstat", return_value=file_stat),
        patch.object(helper.os, "close") as close,
        patch.object(helper.fcntl, "flock", side_effect=BlockingIOError),
    ):
        response = helper.handle_request(
            _request(),
            runner=runner,
            lock_factory=helper._target_lock,  # noqa: SLF001
        )
    assert response["error"]["classification"] == "package_manager_busy"
    assert runner.calls == []
    close.assert_called_once_with(42)


def test_helper_classifies_command_timeout() -> None:
    """A timed-out fixed command returns a bounded timeout failure."""
    response = helper.handle_request(
        _request(),
        runner=lambda _argv, _timeout, _max_output: helper.CommandResult(
            -9, b"", b"", timed_out=True
        ),
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert response["error"] == {
        "classification": "timeout",
        "message": "package scan command timed out",
    }


def test_helper_commands_share_one_global_deadline() -> None:
    """Later commands receive only global operation time which remains."""

    class FakeClock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = FakeClock()
    base_runner = FakeHelperRunner()
    timeouts: list[float] = []

    def advancing_runner(argv, timeout, max_output):
        timeouts.append(timeout)
        result = base_runner(argv, timeout, max_output)
        clock.value += 100.0 if len(timeouts) < 3 else 41.0
        return result

    response = helper.handle_request(
        _request(),
        runner=advancing_runner,
        clock=clock,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert response["error"] == {
        "classification": "timeout",
        "message": "package scan operation deadline exceeded",
    }
    assert timeouts == [120.0, 120.0, 40.0]


@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False), (2, None)])
def test_helper_preserves_reboot_tri_state(
    returncode: int, expected: bool | None
) -> None:
    """Only definitive marker evidence becomes true or false."""
    response = _handle(FakeHelperRunner(reboot_returncode=returncode))
    assert response["evidence"]["reboot_required"] is expected


@pytest.mark.parametrize(
    ("runner", "classification"),
    [
        (FakeHelperRunner(local_node="pve2"), "identity_mismatch"),
        (FakeHelperRunner(config_returncode=1), "guest_unavailable"),
        (FakeHelperRunner(status="stopped"), "guest_unavailable"),
    ],
)
def test_helper_validates_node_and_target_freshness(
    runner: FakeHelperRunner, classification: str
) -> None:
    """Execution fails safely for the wrong node, non-LXC, or stopped LXC."""
    response = _handle(runner)
    assert response["error"]["classification"] == classification
    assert not any("pct exec" in " ".join(call[0]) for call in runner.calls)


def test_helper_rejects_arbitrary_operations_nodes_and_vmids() -> None:
    """The forced command accepts no command text or free-form target values."""
    with pytest.raises(helper.RequestError):
        helper.validate_request({**_request(), "operation": "upgrade_packages"})
    with pytest.raises(helper.RequestError):
        helper.validate_request(_request(vmid=True))
    with pytest.raises(helper.RequestError):
        helper.validate_request(_request(node="../pve1"))
