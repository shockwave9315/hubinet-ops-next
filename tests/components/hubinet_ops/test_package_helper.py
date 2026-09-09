"""Tests for the separately deployed forced-command package helper."""

import ast
from contextlib import contextmanager, nullcontext
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
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
        mutation_returncode: int = 0,
        mutation_stderr: str = "",
        inventory_outputs: tuple[str, ...] | None = None,
        ping_returncode: int = 0,
        reboot_returncode: int = 1,
    ) -> None:
        """Initialize fixed command outcomes."""
        self.local_node = local_node
        self.config_returncode = config_returncode
        self.status = status
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.mutation_returncode = mutation_returncode
        self.mutation_stderr = mutation_stderr
        self.inventory_outputs = inventory_outputs or (TWO_INVENTORY,)
        self.inventory_reads = 0
        self.ping_returncode = ping_returncode
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
        if "apt-get upgrade" in rendered:
            return helper.CommandResult(
                self.mutation_returncode, b"", self.mutation_stderr.encode()
            )
        if "dpkg --print-architecture" in rendered:
            return helper.CommandResult(0, b"amd64\n", b"")
        if "dpkg-query" in rendered:
            output = self.inventory_outputs[
                min(self.inventory_reads, len(self.inventory_outputs) - 1)
            ]
            self.inventory_reads += 1
            return helper.CommandResult(0, output.encode(), b"")
        if "/var/run/reboot-required" in rendered:
            return helper.CommandResult(self.reboot_returncode, b"", b"")
        if argv[-1:] == ("/bin/true",):
            return helper.CommandResult(self.ping_returncode, b"", b"")
        raise AssertionError(f"unexpected command: {argv!r}")


def _request(
    vmid: int = 200, node: str = "pve1", operation: str = "scan_packages"
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "operation": operation,
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
    assert response["helper_version"] == 3
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
    assert any(command[4:] == helper.APT_SIMULATION_COMMAND for command in guest_commands)


def test_probe_returns_local_node_and_never_calls_pct() -> None:
    """The setup probe performs only PVE-native local-node identification."""
    runner = FakeHelperRunner()
    response = helper.handle_request(
        {"protocol_version": 1, "operation": "probe"}, runner=runner
    )
    assert response == {
        "protocol_version": 1,
        "helper_version": 3,
        "operation": "probe",
        "ok": True,
        "node": "pve1",
    }
    assert [call[0] for call in runner.calls] == [
        ("readlink", "-f", "/etc/pve/local")
    ]


def test_scan_plan_and_mutation_share_hardened_apt_policy() -> None:
    """Scan/plan/mutation cannot diverge because of guest apt configuration."""
    scan_runner = FakeHelperRunner()
    assert _handle(scan_runner)["ok"] is True

    plan_runner = FakeHelperRunner()
    plan = helper.handle_request(
        _request(operation="plan_packages"),
        runner=plan_runner,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert plan["ok"] is True

    update_runner = FakeHelperRunner(
        inventory_outputs=(
            TWO_INVENTORY,
            "openssl\tamd64\t3.0.11-2\tinstalled\napt\tamd64\t2.6.2\tinstalled\n",
        )
    )
    update = helper.handle_request(
        _request(operation="update_packages"),
        runner=update_runner,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert update["ok"] is True

    scan_simulation = next(
        argv for argv, *_ in scan_runner.calls if argv[4:] == helper.APT_SIMULATION_COMMAND
    )
    plan_simulation = next(
        argv for argv, *_ in plan_runner.calls if argv[4:] == helper.APT_SIMULATION_COMMAND
    )
    mutation = next(
        argv for argv, *_ in update_runner.calls if argv[4:] == helper.APT_MUTATION_COMMAND
    )
    assert scan_simulation[4:] == plan_simulation[4:] == helper.APT_SIMULATION_COMMAND
    assert "APT::Ignore-Hold=false" in scan_simulation
    assert mutation[4:] == helper.APT_MUTATION_COMMAND
    assert helper.APT_SIMULATION_COMMAND[-len(helper.APT_HARDENED_OPTIONS) :] == (
        helper.APT_MUTATION_COMMAND[-len(helper.APT_HARDENED_OPTIONS) :]
    )


def test_plan_uses_current_apt_lists_without_refresh_or_version_gate() -> None:
    """Execution-time planning simulates only; it never imports new metadata."""
    runner = FakeHelperRunner()
    response = helper.handle_request(
        _request(operation="plan_packages"),
        runner=runner,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert response["ok"] is True
    rendered = [" ".join(call[0]) for call in runner.calls]
    assert not any("apt-get update" in command for command in rendered)
    assert not any("apt-get --version" in command for command in rendered)
    assert not any("/etc/os-release" in command for command in rendered)
    assert sum(call[0][4:] == helper.APT_SIMULATION_COMMAND for call in runner.calls) == 1


def test_update_uses_one_fixed_bare_upgrade_without_plan_material() -> None:
    """Mutation argv is fixed and contains no caller package/version material."""
    runner = FakeHelperRunner()
    request = _request(operation="update_packages")
    assert set(request) == {"protocol_version", "operation", "target"}
    response = helper.handle_request(
        request,
        runner=runner,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert response["ok"] is True
    mutations = [
        call for call in runner.calls if call[0][4:] == helper.APT_MUTATION_COMMAND
    ]
    assert len(mutations) == 1
    argv, timeout, _max_output = mutations[0]
    assert argv == ("pct", "exec", "200", "--", *helper.APT_MUTATION_COMMAND)
    assert timeout == helper.UPDATE_COMMAND_TIMEOUT_SECONDS
    assert "openssl" not in argv
    assert "3.0.11-1~deb12u3" not in argv
    assert not any("apt-get update" in " ".join(call[0]) for call in runner.calls)


def test_fixed_upgrade_policy_keeps_held_packages_and_forbids_unsafe_apt_modes() -> None:
    """The audited bare upgrade shape explicitly overrides guest hold policy."""
    assert helper.APT_MUTATION_COMMAND == (
        "env",
        "LC_ALL=C",
        "DEBIAN_FRONTEND=noninteractive",
        "apt-get",
        "upgrade",
        "-y",
        "-o",
        "APT::Get::Upgrade-Allow-New=false",
        "-o",
        "APT::Get::Remove=false",
        "-o",
        "APT::Get::Force-Yes=false",
        "-o",
        "APT::Get::allow-downgrades=false",
        "-o",
        "APT::Get::allow-remove-essential=false",
        "-o",
        "APT::Get::allow-change-held-packages=false",
        "-o",
        "APT::Get::AllowUnauthenticated=false",
        "-o",
        "APT::Ignore-Hold=false",
        "-o",
        "Dpkg::Options::=--force-confdef",
        "-o",
        "Dpkg::Options::=--force-confold",
    )


@pytest.mark.parametrize(
    "operation", ["scan_packages", "plan_packages", "update_packages"]
)
def test_all_package_operations_use_same_vmid_lock(operation: str) -> None:
    """Every package operation takes the unchanged per-VMID lock boundary."""
    locked: list[int] = []

    @contextmanager
    def lock(vmid: int):
        locked.append(vmid)
        yield

    response = helper.handle_request(
        _request(operation=operation), runner=FakeHelperRunner(), lock_factory=lock
    )
    assert response["ok"] is True
    assert locked == [200]


def test_update_fails_closed_on_unfinished_inventory_before_mutation() -> None:
    """Unfinished dpkg state is detected before apt-get can mutate anything."""
    unfinished = "openssl\tamd64\t3.0.11-1\tunpacked\n"
    runner = FakeHelperRunner(inventory_outputs=(unfinished,))
    response = helper.handle_request(
        _request(operation="update_packages"),
        runner=runner,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert response["error"]["classification"] == "dpkg_unfinished"
    assert not any(call[0][4:] == helper.APT_MUTATION_COMMAND for call in runner.calls)


@pytest.mark.parametrize(
    ("runner", "classification"),
    [
        (
            FakeHelperRunner(
                mutation_returncode=100,
                mutation_stderr="E: Could not get lock /var/lib/dpkg/lock-frontend",
            ),
            "package_manager_busy",
        ),
        (FakeHelperRunner(mutation_returncode=100), "mutation_failed"),
    ],
)
def test_update_classifies_busy_and_apt_rc_100(
    runner: FakeHelperRunner, classification: str
) -> None:
    """APT mutation failures remain bounded and semantic."""
    response = helper.handle_request(
        _request(operation="update_packages"),
        runner=runner,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert response["error"]["classification"] == classification


def test_update_timeout_and_post_mutation_inventory_failures_are_bounded() -> None:
    """Timeout, malformed inventory, and unfinished post-state all fail closed."""

    def timeout_runner(argv, timeout, max_output):
        result = FakeHelperRunner()(argv, timeout, max_output)
        if argv[4:] == helper.APT_MUTATION_COMMAND:
            return helper.CommandResult(-9, b"", b"", timed_out=True)
        return result

    timed_out = helper.handle_request(
        _request(operation="update_packages"),
        runner=timeout_runner,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert timed_out["error"]["classification"] == "timeout"

    for after, classification in (
        ("malformed\n", "dpkg_sanity_failed"),
        ("openssl\tamd64\t3.0.11-2\thalf-configured\n", "dpkg_unfinished"),
    ):
        response = helper.handle_request(
            _request(operation="update_packages"),
            runner=FakeHelperRunner(inventory_outputs=(TWO_INVENTORY, after)),
            lock_factory=lambda _vmid: nullcontext(),
        )
        assert response["error"]["classification"] == classification


def test_protocol_and_request_bound_are_unchanged() -> None:
    """New typed operations do not enlarge or negotiate the request protocol."""
    assert helper.PROTOCOL_VERSION == 1
    assert helper.MAX_REQUEST_BYTES == 1024
    for operation in ("scan_packages", "plan_packages", "update_packages"):
        assert len(json.dumps(_request(operation=operation)).encode()) <= 1024


def test_package_update_source_keeps_snapshot_and_recovery_out_of_helper() -> None:
    """Static guardrails preserve the accepted helper and native-PVE boundary."""
    helper_source = HELPER_PATH.read_text()
    package_source = "\n".join(
        path.read_text()
        for path in (
            REPO_ROOT / "custom_components/hubinet_ops/packages/manager.py",
            REPO_ROOT / "custom_components/hubinet_ops/packages/snapshots.py",
            REPO_ROOT / "custom_components/hubinet_ops/packages/transport.py",
        )
    )
    assert "snapshot" not in helper_source.casefold()
    for forbidden in (
        "pvesh",
        "pct snapshot",
        "pct listsnapshot",
        "dpkg --configure -a",
        "DPkg::Pre-Install-Pkgs",
        "shell=True",
    ):
        assert forbidden not in helper_source
        assert forbidden not in package_source
    assert "rollback" not in package_source.casefold()
    assert (
        'path = f"{LOCK_DIRECTORY}/hubinet-ops-package-scan-{vmid}.lock"'
        in helper_source
    )


def test_package_update_source_has_no_application_health_probes() -> None:
    """The feature stops at native running state and fixed /bin/true PONG."""
    source = "\n".join(
        path.read_text().casefold()
        for path in (
            HELPER_PATH,
            REPO_ROOT / "custom_components/hubinet_ops/packages/manager.py",
            REPO_ROOT / "custom_components/hubinet_ops/packages/snapshots.py",
            REPO_ROOT / "custom_components/hubinet_ops/packages/transport.py",
        )
    )
    for forbidden in (
        "curl",
        "wget",
        "systemctl",
        "docker",
        "mqtt",
        "adguard",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(("returncode", "ok"), [(0, True), (1, False)])
def test_ping_is_only_fixed_bin_true(returncode: int, ok: bool) -> None:
    """Generic liveness is one typed fixed /bin/true operation."""
    runner = FakeHelperRunner(ping_returncode=returncode)
    response = helper.handle_request(
        _request(operation="ping"),
        runner=runner,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert response["ok"] is ok
    probes = [call for call in runner.calls if call[0][-1:] == ("/bin/true",)]
    assert len(probes) == 1
    assert probes[0][0] == ("pct", "exec", "200", "--", "/bin/true")
    assert probes[0][1] == helper.PING_COMMAND_TIMEOUT_SECONDS
    assert not any(
        token in " ".join(call[0])
        for token in ("curl", "systemctl", "docker", "ping", "dig")
        for call in runner.calls
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol_version": 1, "operation": "probe", "target": {}},
        {"protocol_version": 2, "operation": "probe"},
        {"protocol_version": 1, "operation": "unknown"},
    ],
)
def test_malformed_probe_and_protocol_mismatch_are_rejected(payload) -> None:
    """Probe accepts only its exact versioned request shape."""
    with pytest.raises(helper.RequestError):
        helper.handle_request(payload, runner=FakeHelperRunner())


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
    major, _minor, _patch = helper._parse_apt_version(f"{apt_version_line}\n")  # noqa: SLF001
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


def test_run_bounded_cleanup_wait_timeout_kills_the_process_group() -> None:
    """N4: a genuine cleanup-wait timeout must not leave the process running.

    The child closes its own stdout/stderr immediately (letting the read
    loop finish right away) but keeps running well past the 5s cleanup
    grace period, so `process.wait(timeout=5)` genuinely raises
    TimeoutExpired -- not a mocked one. `_run_bounded()` must still return
    a structured timeout, and the process (and its process group) must
    actually be gone afterward, not merely classified as failed.
    """
    created: list[subprocess.Popen] = []
    real_popen = helper.subprocess.Popen

    def _capturing_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process

    with patch.object(helper.subprocess, "Popen", side_effect=_capturing_popen):
        result = helper._run_bounded(  # noqa: SLF001
            ("bash", "-c", "exec 1>&- 2>&-; sleep 12"), 20.0, 4096
        )
    assert result.timed_out is True

    assert len(created) == 1
    process = created[0]
    for _ in range(50):
        if process.poll() is not None:
            break
        time.sleep(0.05)
    assert process.poll() is not None, "child survived _run_bounded() returning"
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


def test_helper_source_is_python_3_11_compatible() -> None:
    """N9: the helper's grammar must not require Python 3.12+.

    A Python 3.11 interpreter was not available in this environment.
    ``py_compile`` under the repository's Python 3.14 runtime only proves
    the source is valid *current*-Python syntax, not 3.11 syntax -- so the
    real regression is ``ast.parse`` with ``feature_version=(3, 11)``,
    which rejects constructs newer than that grammar (for example, a PEP
    695 ``type`` statement fails with exactly this feature_version). PEP
    695 `type` statements were replaced with conventional assignment
    aliases; this also stands as a compile-error check.
    """
    ast.parse(HELPER_PATH.read_text(), feature_version=(3, 11))
