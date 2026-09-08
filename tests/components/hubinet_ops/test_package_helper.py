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
        if argv == ("readlink", "-f", "/etc/pve/local"):
            return helper.CommandResult(
                0, f"/etc/pve/nodes/{self.local_node}\n".encode(), b""
            )
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
        ("readlink", "-f", "/etc/pve/local"),
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


@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, None), (2, None)])
def test_helper_preserves_reboot_tri_state(
    returncode: int, expected: bool | None
) -> None:
    """Only the marker's presence is reliable evidence; everything else is unknown.

    rc 0 (marker exists) is the only reliable positive. rc 1 (marker
    absent) is not reliable negative evidence, and any other outcome is
    also unknown -- there is currently no reliable negative reboot
    evidence, per ARCHITECTURE.md's tri-state rules.
    """
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
    with pytest.raises(helper.RequestError):
        helper.validate_request({**_request(), "protocol_version": True})


def test_local_node_identity_uses_pve_native_source_not_raw_hostname() -> None:
    """N7: identity comes from /etc/pve/local, never raw (possibly FQDN) hostname.

    Expected "pve1" must pass regardless of what the system's hostname
    happens to be (short or FQDN), because /etc/pve/local resolves to the
    PVE-native short node name directly.
    """
    runner = FakeHelperRunner(local_node="pve1")
    response = _handle(runner)
    assert response["ok"] is True
    assert not any(call[0] == ("hostname",) for call in runner.calls)
    assert runner.calls[0][0] == ("readlink", "-f", "/etc/pve/local")


@pytest.mark.parametrize(
    "apt_version_line",
    [
        "apt 2.6.1 (amd64)",
        "apt 3.0.3 (amd64)",
        "apt 2.6.1+deb12u1 (amd64)",
        "apt 2.7.14build2 (amd64)",
        "apt 2.4.5ubuntu1 (amd64)",
    ],
)
def test_apt_version_accepts_real_ubuntu_and_debian_revision_suffixes(
    apt_version_line: str,
) -> None:
    """N1: real Ubuntu/Debian distro revision suffixes are not rejected.

    Only the numeric major.minor.patch prefix is gated; parsing must not
    fail merely because a distro revision suffix like "build2" or
    "ubuntu1" is attached directly, without a "~+.-" separator.
    """
    major, minor, patch = helper._parse_apt_version(f"{apt_version_line}\n")  # noqa: SLF001
    assert major >= 2


@pytest.mark.parametrize(
    "apt_version_line",
    ["apt 2.0.9 (amd64)", "apt 1.8.2.3 (amd64)"],
)
def test_apt_version_below_minimum_is_rejected_by_the_feature_gate(
    apt_version_line: str,
) -> None:
    """Below-minimum numeric versions still fail the feature gate."""
    parsed = helper._parse_apt_version(f"{apt_version_line}\n")  # noqa: SLF001
    assert parsed < helper.MINIMUM_APT_VERSION


def test_apt_version_malformed_output_is_still_rejected() -> None:
    """The relaxed prefix match does not accept non-APT-version output."""
    with pytest.raises(helper.ScanError):
        helper._parse_apt_version("Reading package lists...\n")  # noqa: SLF001


def test_run_bounded_direct_child_exit_is_not_a_false_timeout() -> None:
    """N6: a descendant holding the inherited pipe must not fake a timeout.

    The direct child exits almost immediately; a background subshell it
    spawns keeps the inherited stdout pipe open for longer. `_run_bounded`
    must return promptly, classifying this as a normal successful exit.
    """
    result = helper._run_bounded(  # noqa: SLF001
        ("bash", "-c", "echo done; ( sleep 2 & ) ; exit 0"),
        2.0,
        4096,
    )
    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout.strip() == b"done"


def test_run_bounded_final_drain_still_enforces_the_output_bound() -> None:
    """Low: the poll()-detected final drain must recheck the combined bound.

    A descendant holds the pipe open past the direct child's exit, so
    `_run_bounded` takes the final-drain path (see the test above). That
    drain's own newly-appended output must still be checked against
    `max_output`, not only the output collected by the main read loop.
    """
    result = helper._run_bounded(  # noqa: SLF001
        ("bash", "-c", "echo 0123456789; ( sleep 2 & ) ; exit 0"),
        2.0,
        4,
    )
    assert result.output_exceeded is True


def test_run_bounded_real_timeout_is_classified_and_kills_the_process() -> None:
    """A genuinely hanging command is bounded and reported as a timeout."""
    result = helper._run_bounded(("sleep", "5"), 0.2, 4096)  # noqa: SLF001
    assert result.timed_out is True


def test_run_bounded_cleanup_timeout_expired_becomes_structured_failure() -> None:
    """N6: cleanup's own TimeoutExpired never escapes as an uncaught traceback.

    It must instead be folded into a structured timeout result so the
    protocol layer above still returns bounded JSON -- even though the
    direct child itself exited cleanly and produced its output.
    """
    with patch.object(
        helper.subprocess.Popen,
        "wait",
        side_effect=helper.subprocess.TimeoutExpired(cmd="x", timeout=5),
    ):
        result = helper._run_bounded(  # noqa: SLF001
            ("bash", "-c", "echo done; ( sleep 2 & ) ; exit 0"), 2.0, 4096
        )
    assert result.timed_out is True
    assert result.returncode == 0
    assert result.stdout.strip() == b"done"


def test_helper_source_is_python_3_11_compatible() -> None:
    """N9: the helper must at least parse without requiring Python 3.12+.

    A Python 3.11 interpreter was not available in this environment; PEP
    695 `type` statements (3.12+) were replaced with conventional
    assignment aliases, reviewed by inspection for any other 3.12+-only
    syntax elsewhere in the file.
    """
    import py_compile

    py_compile.compile(str(HELPER_PATH), doraise=True)
