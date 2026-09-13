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

from custom_components.hubinet_ops.const import EXPECTED_HELPER_VERSION
from custom_components.hubinet_ops.packages.models import (
    HealthReason,
    HealthState,
    classify_health,
)
from custom_components.hubinet_ops.packages.transport import (
    TRANSPORT_TIMEOUT_SECONDS,
    _parse_health_response,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = REPO_ROOT / "deploy/hubinet-package-scan-helper.py"

TWO_UPDATES = """\
Inst openssl [3.0.11-1] (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Inst apt [2.6.1] (2.6.2 Debian:12/oldstable [amd64])
2 upgraded, 0 newly installed, 0 to remove and 41 not upgraded.
"""
TWO_INVENTORY = "openssl\tamd64\t3.0.11-1\tinstalled\napt\tamd64\t2.6.1\tinstalled\n"
AUTOREMOVE_SIMULATION = (
    "Remv libslirp0 [4.7.0-1]\n"
    "0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n"
)


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("package_scan_helper", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def test_expected_helper_version_matches_shipped_helper() -> None:
    """Release stale-helper UX matches the helper actually shipped."""
    assert EXPECTED_HELPER_VERSION == helper.HELPER_VERSION


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
        autoremove_simulation: str = AUTOREMOVE_SIMULATION,
        inventory_outputs: tuple[str, ...] | None = None,
        ping_returncode: int = 0,
        reboot_returncode: int = 1,
        audit_returncode: int = 0,
        audit_stdout: str = "",
        audit_stdout_bytes: bytes | None = None,
        status_after_exec_failure: str | None = None,
    ) -> None:
        """Initialize fixed command outcomes."""
        self.local_node = local_node
        self.config_returncode = config_returncode
        self.status = status
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.mutation_returncode = mutation_returncode
        self.mutation_stderr = mutation_stderr
        self.autoremove_simulation = autoremove_simulation
        self.inventory_outputs = inventory_outputs or (TWO_INVENTORY,)
        self.inventory_reads = 0
        self.ping_returncode = ping_returncode
        self.reboot_returncode = reboot_returncode
        self.audit_returncode = audit_returncode
        self.audit_stdout = audit_stdout
        self.audit_stdout_bytes = audit_stdout_bytes
        self.status_after_exec_failure = status_after_exec_failure
        self.status_calls = 0
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
            self.status_calls += 1
            status = self.status
            if self.status_calls > 1 and self.status_after_exec_failure is not None:
                status = self.status_after_exec_failure
            return helper.CommandResult(0, f"status: {status}\n".encode(), b"")
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
        if "apt-get -s autoremove" in rendered:
            return helper.CommandResult(0, self.autoremove_simulation.encode(), b"")
        if "apt-get autoremove" in rendered:
            return helper.CommandResult(
                self.mutation_returncode, b"", self.mutation_stderr.encode()
            )
        if "apt-get upgrade" in rendered:
            return helper.CommandResult(
                self.mutation_returncode, b"", self.mutation_stderr.encode()
            )
        if "dpkg --audit" in rendered:
            audit_stdout = (
                self.audit_stdout_bytes
                if self.audit_stdout_bytes is not None
                else self.audit_stdout.encode()
            )
            return helper.CommandResult(self.audit_returncode, audit_stdout, b"")
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
    assert response["helper_version"] == helper.HELPER_VERSION
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
        "helper_version": helper.HELPER_VERSION,
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


def test_cleanup_operations_use_one_fixed_safe_policy_and_existing_lock() -> None:
    """Plan and mutation pin removal semantics without caller package argv."""
    plan_runner = FakeHelperRunner(
        inventory_outputs=("libslirp0\tamd64\t4.7.0-1\tinstalled\n",)
    )
    locked: list[int] = []

    @contextmanager
    def lock(vmid: int):
        locked.append(vmid)
        yield

    plan = helper.handle_request(
        _request(operation="plan_autoremove"),
        runner=plan_runner,
        lock_factory=lock,
    )
    assert plan["ok"] is True
    assert set(plan["evidence"]) == {
        "native_architecture",
        "installed_inventory",
        "autoremove_simulation",
    }

    mutation_runner = FakeHelperRunner(
        inventory_outputs=(
            "libslirp0\tamd64\t4.7.0-1\tinstalled\n",
            "",
        )
    )
    mutation = helper.handle_request(
        _request(operation="autoremove_packages"),
        runner=mutation_runner,
        lock_factory=lock,
    )
    assert mutation["ok"] is True
    assert locked == [200, 200]

    plan_command = next(
        argv
        for argv, *_ in plan_runner.calls
        if argv[4:] == helper.APT_AUTOREMOVE_SIMULATION_COMMAND
    )
    mutation_command = next(
        argv
        for argv, *_ in mutation_runner.calls
        if argv[4:] == helper.APT_AUTOREMOVE_MUTATION_COMMAND
    )
    required = {
        "APT::Get::Remove=true",
        "APT::Get::Purge=false",
        "APT::Protect-Kernels=true",
        "APT::Ignore-Hold=false",
        "APT::Get::allow-remove-essential=false",
        "APT::Get::allow-change-held-packages=false",
        "APT::Get::AllowUnauthenticated=false",
        "APT::Get::Force-Yes=false",
        "APT::Get::allow-downgrades=false",
    }
    assert required <= set(plan_command)
    assert required <= set(mutation_command)
    assert plan_command[-len(helper.APT_CLEANUP_OPTIONS) :] == (
        mutation_command[-len(helper.APT_CLEANUP_OPTIONS) :]
    )
    for command in (plan_command, mutation_command):
        assert "APT::Get::Remove=false" not in command
        assert "--purge" not in command
        assert "libslirp0" not in command
        assert "APT::Get::Upgrade-Allow-New" not in command
        assert not any(value.startswith("Dpkg::Options") for value in command)
        assert "update" not in command


def test_cleanup_operation_requests_have_no_package_input() -> None:
    """The exact existing envelope cannot carry a deletion plan or argv."""
    for operation in ("plan_autoremove", "autoremove_packages"):
        request = _request(operation=operation)
        assert helper.validate_request(request) == (operation, "pve1", 200)
        with pytest.raises(helper.RequestError):
            helper.validate_request({**request, "packages": ["libslirp0"]})


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
    "operation",
    ["scan_packages", "plan_packages", "update_packages", "check_health"],
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


def _health(runner: FakeHelperRunner, **kwargs):
    """Run one ``check_health`` request through the fixed dispatch."""
    return helper.handle_request(
        _request(operation="check_health"),
        runner=runner,
        lock_factory=lambda _vmid: nullcontext(),
        **kwargs,
    )


def test_check_health_happy_path_is_bounded_and_versioned() -> None:
    """A clean, running guest returns bounded evidence with no reboot marker."""
    runner = FakeHelperRunner(reboot_returncode=1)
    response = _health(runner)
    assert response["ok"] is True
    assert response["helper_version"] == helper.HELPER_VERSION
    assert response["operation"] == "check_health"
    assert response["target"] == {"node": "pve1", "vmid": 200}
    assert response["evidence"] == {
        "guest_exec": True,
        "guest_exec_unavailable": False,
        "dpkg": "ok",
        "unfinished_package_count": None,
        "reboot_required": None,
    }
    # No package names, dpkg stdout, or other guest-controlled text leaks out.
    assert "openssl" not in json.dumps(response)


def test_check_health_identity_mismatch_is_unestablished_not_failed() -> None:
    """A wrong local node is a classified failure, never a FAILED verdict."""
    response = _health(FakeHelperRunner(local_node="pve2"))
    assert response["ok"] is False
    assert response["error"]["classification"] == "identity_mismatch"


def test_check_health_stopped_target_is_unestablished_not_failed() -> None:
    """A stopped/unavailable target never becomes FAILED Health evidence."""
    runner = FakeHelperRunner(status="stopped")
    response = _health(runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "guest_unavailable"
    assert not any(call[0][-1:] == ("/bin/true",) for call in runner.calls)


def test_check_health_exec_failure_while_still_running_is_positive_failure() -> None:
    """A guest that answers pct status but fails /bin/true is FAILED evidence."""
    runner = FakeHelperRunner(ping_returncode=1, status="running")
    response = _health(runner)
    assert response["ok"] is True
    assert response["evidence"] == {
        "guest_exec": False,
        "guest_exec_unavailable": False,
        "dpkg": None,
        "unfinished_package_count": None,
        "reboot_required": None,
    }
    # dpkg/reboot checks are skipped once guest_exec is false.
    assert not any("dpkg" in " ".join(call[0]) for call in runner.calls)
    assert not any(
        "/var/run/reboot-required" in " ".join(call[0]) for call in runner.calls
    )


def test_check_health_exec_failure_after_guest_stops_is_unknown() -> None:
    """A guest that stops between validation and exec is UNKNOWN, not FAILED."""
    runner = FakeHelperRunner(
        ping_returncode=1, status="running", status_after_exec_failure="stopped"
    )
    response = _health(runner)
    assert response["ok"] is True
    assert response["evidence"]["guest_exec"] is False
    assert response["evidence"]["guest_exec_unavailable"] is True


def test_check_health_exec_timeout_is_bounded_timeout_failure() -> None:
    """A hanging fixed exec command is a bounded timeout, never a hang."""

    def timeout_runner(argv, timeout, max_output):
        if argv[-1:] == ("/bin/true",):
            return helper.CommandResult(-9, b"", b"", timed_out=True)
        return FakeHelperRunner()(argv, timeout, max_output)

    response = _health(timeout_runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "timeout"


def test_check_health_dpkg_ok_with_no_unfinished_state() -> None:
    """A fully installed inventory is dpkg ``ok`` with no audit call at all."""
    runner = FakeHelperRunner(inventory_outputs=(TWO_INVENTORY,))
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "ok"
    assert response["evidence"]["unfinished_package_count"] is None
    assert not any("dpkg --audit" in " ".join(call[0]) for call in runner.calls)


# Verbatim LC_ALL=C ``dpkg --audit`` output (dpkg 1.22) for a half-* database.
AUDIT_HALF_INSTALLED = (
    "The following packages are only half installed, due to problems during\n"
    "installation.  The installation can probably be completed by retrying it;\n"
    "the packages can be removed using dselect or dpkg --remove:\n"
    " openssl              Secure Sockets Layer toolkit\n\n"
)
AUDIT_HALF_CONFIGURED = (
    "The following packages are only half configured, probably due to problems\n"
    "configuring them the first time.  The configuration should be retried using\n"
    "dpkg --configure <package> or the configure menu option in dselect:\n"
    " openssl              Secure Sockets Layer toolkit\n\n"
)
AUDIT_LOCKED_PREFIX = (
    "Another process has locked the database for writing, and might currently be\n"
    "modifying it, some of the following problems might just be due to that.\n\n"
)


def test_check_health_persistent_half_state_is_interrupted() -> None:
    """A half-installed identity that survives a second read is FAILED evidence."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    runner = FakeHelperRunner(
        inventory_outputs=(half, half), audit_stdout=AUDIT_HALF_INSTALLED
    )
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "interrupted"
    assert response["evidence"]["unfinished_package_count"] == 1
    audit_calls = [
        call for call in runner.calls if "dpkg --audit" in " ".join(call[0])
    ]
    assert len(audit_calls) == 1


def test_check_health_changed_identity_between_reads_is_unknown() -> None:
    """A half-installed identity that resolved by the second read is UNKNOWN."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    resolved = "openssl\tamd64\t3.0.11-1\tinstalled\n"
    runner = FakeHelperRunner(inventory_outputs=(half, resolved))
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "changed"
    assert response["evidence"]["unfinished_package_count"] is None


def test_check_health_pending_only_is_never_failed() -> None:
    """Packages merely unpacked/triggers-pending are UNKNOWN, never FAILED.

    The lock is still audited for busy/uncertain disambiguation even when
    only pending identities exist, but a clean audit result here always
    classifies as pending -- a P-only inventory is never a second-read
    candidate and can never become FAILED.
    """
    pending = "openssl\tamd64\t3.0.11-1\tunpacked\n"
    runner = FakeHelperRunner(inventory_outputs=(pending,))
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "pending"
    assert response["evidence"]["unfinished_package_count"] is None
    assert any("dpkg --audit" in " ".join(call[0]) for call in runner.calls)
    # Only one inventory read -- a P-only result never re-reads.
    assert runner.inventory_reads == 1


@pytest.mark.parametrize(
    ("audit", "expected"),
    [
        ({}, "pending"),
        (
            {
                "audit_stdout": (
                    "openssl:\n Another process has locked the database for writing\n"
                )
            },
            "busy",
        ),
        ({"audit_returncode": 2}, "lock_unknown"),
        ({"audit_stdout_bytes": b"\xff\xfe not utf-8"}, "lock_unknown"),
    ],
)
def test_check_health_pending_only_is_one_read_and_audit_uncertainty_wins(
    audit: dict[str, object], expected: str
) -> None:
    """A P-only inventory is read once; busy/uncertain audit wins over pending.

    Even if the guest would have finished the package by a hypothetical
    second read, the result stays an UNKNOWN-only classification: P-only
    evidence can never become FAILED or a false HEALTHY ``ok``.
    """
    pending = "openssl\tamd64\t3.0.11-1\ttriggers-pending\n"
    resolved = "openssl\tamd64\t3.0.11-1\tinstalled\n"
    runner = FakeHelperRunner(inventory_outputs=(pending, resolved), **audit)
    response = _health(runner)
    assert response["evidence"]["dpkg"] == expected
    assert response["evidence"]["unfinished_package_count"] is None
    assert runner.inventory_reads == 1


def test_check_health_audit_lock_notice_is_package_manager_busy() -> None:
    """The fixed LC_ALL=C lock notice is disambiguated as busy, not FAILED."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    runner = FakeHelperRunner(
        inventory_outputs=(half,),
        audit_stdout=(
            "openssl:\n"
            " Another process has locked the database for writing\n"
        ),
    )
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "busy"


def test_check_health_audit_failure_is_lock_unknown() -> None:
    """A nonzero dpkg --audit result cannot be evaluated reliably."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    runner = FakeHelperRunner(inventory_outputs=(half,), audit_returncode=2)
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "lock_unknown"


def test_check_health_progressed_half_state_is_changed_not_interrupted() -> None:
    """Progress between two half-* states is CHANGED, not a persistent failure.

    half-installed -> half-configured is forward progress, not the same
    stuck state; requiring the *exact* original status (not merely "still
    somewhere in the half-* set") across both reads prevents this from
    being misclassified as FAILED.
    """
    half_installed = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    half_configured = "openssl\tamd64\t3.0.11-1\thalf-configured\n"
    runner = FakeHelperRunner(inventory_outputs=(half_installed, half_configured))
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "changed"
    assert response["evidence"]["unfinished_package_count"] is None


def test_check_health_resolved_half_configured_state_is_changed() -> None:
    """A half-configured identity that resolves by the second read is CHANGED."""
    half_configured = "openssl\tamd64\t3.0.11-1\thalf-configured\n"
    installed = "openssl\tamd64\t3.0.11-1\tinstalled\n"
    runner = FakeHelperRunner(inventory_outputs=(half_configured, installed))
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "changed"


def test_check_health_second_read_precedes_audit_so_busy_wins_over_persisted() -> None:
    """A busy lock observed at audit time (after the second read) is never FAILED.

    Even though both reads show the identical half-installed state, dpkg
    could still be actively working on it right up to the audit; the busy
    result at that point takes precedence over the read comparison.
    """
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    runner = FakeHelperRunner(
        inventory_outputs=(half, half),
        audit_stdout=(
            "openssl:\n Another process has locked the database for writing\n"
        ),
    )
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "busy"
    assert response["evidence"]["unfinished_package_count"] is None
    # The order is read, re-read, then audit.
    dpkg_query_indices = [
        index
        for index, call in enumerate(runner.calls)
        if "dpkg-query" in " ".join(call[0])
    ]
    audit_index = next(
        index
        for index, call in enumerate(runner.calls)
        if "dpkg --audit" in " ".join(call[0])
    )
    assert len(dpkg_query_indices) == 2
    assert max(dpkg_query_indices) < audit_index


@pytest.mark.parametrize(
    ("half_status", "audit_stdout", "expected"),
    [
        # Lock held: dpkg's own notice precedes its half-* report.
        ("half-installed", AUDIT_LOCKED_PREFIX + AUDIT_HALF_INSTALLED, "busy"),
        ("half-configured", AUDIT_LOCKED_PREFIX + AUDIT_HALF_CONFIGURED, "busy"),
        # Lock tested and clear: audit itself still reports the same state.
        ("half-installed", AUDIT_HALF_INSTALLED, "interrupted"),
        ("half-configured", AUDIT_HALF_CONFIGURED, "interrupted"),
        # Audit's snapshot has no problem, so dpkg never tested the lock; a
        # live writer may hold it (reproduced with a real fcntl lock + rc 0).
        ("half-installed", "", "changed"),
        # A different half-* state is not audit's report of the persisted one.
        ("half-installed", AUDIT_HALF_CONFIGURED, "changed"),
    ],
)
def test_check_health_clear_lock_requires_audit_to_report_the_half_state(
    half_status: str, audit_stdout: str, expected: str
) -> None:
    """dpkg --audit is not a lock probe: a missing busy notice alone is not FAILED.

    ``dpkg --audit`` opens the database read-only, always exits 0, and runs
    its fcntl F_GETLK check on the database lock only after its own
    snapshot finds a problem. Identical reads plus a silent audit therefore
    cannot prove that no writer still holds the lock.
    """
    half = f"openssl\tamd64\t3.0.11-1\t{half_status}\n"
    runner = FakeHelperRunner(inventory_outputs=(half, half), audit_stdout=audit_stdout)
    response = _health(runner)
    assert response["evidence"]["dpkg"] == expected
    assert response["evidence"]["unfinished_package_count"] == (
        1 if expected == "interrupted" else None
    )


# Verbatim LC_ALL=C ``dpkg --audit`` output (dpkg 1.22.22) for a reinst-required
# half-* package (half-installed reproduced by SIGKILLing a real ``dpkg
# --unpack``; half-configured confirmed against a reinst-required status
# database): dpkg lists it only under this section, never under its half-*
# header.
AUDIT_REINSTREQ = (
    "The following packages are in a mess due to serious problems during\n"
    "installation.  They must be reinstalled for them (and any packages\n"
    "that depend on them) to function properly:\n"
    " openssl              Secure Sockets Layer toolkit\n\n"
)


@pytest.mark.parametrize(
    ("half_status", "audit_stdout", "expected"),
    [
        ("half-installed", AUDIT_REINSTREQ, "interrupted"),
        ("half-configured", AUDIT_REINSTREQ, "interrupted"),
        ("half-installed", AUDIT_LOCKED_PREFIX + AUDIT_REINSTREQ, "busy"),
    ],
)
def test_check_health_reinst_required_audit_section_confirms_persisted_half_state(
    half_status: str, audit_stdout: str, expected: str
) -> None:
    """A persisted reinst-required half-* state is interrupted, not changed."""
    half = f"openssl\tamd64\t3.0.11-1\t{half_status}\n"
    runner = FakeHelperRunner(inventory_outputs=(half, half), audit_stdout=audit_stdout)
    response = _health(runner)
    assert response["evidence"]["dpkg"] == expected
    assert response["evidence"]["unfinished_package_count"] == (
        1 if expected == "interrupted" else None
    )


def test_check_health_changed_between_reads_is_changed_regardless_of_audit() -> None:
    """A half-* state that moved between reads is never FAILED, even if audit agrees."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    resolved = "openssl\tamd64\t3.0.11-1\tinstalled\n"
    runner = FakeHelperRunner(
        inventory_outputs=(half, resolved), audit_stdout=AUDIT_HALF_INSTALLED
    )
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "changed"


@pytest.mark.parametrize(
    "audit",
    [
        {"audit_returncode": 2, "audit_stdout": AUDIT_HALF_INSTALLED},
        {"audit_stdout_bytes": b"\xff\xfe not utf-8"},
    ],
)
def test_check_health_audit_error_with_persisted_half_state_is_never_failed(
    audit: dict[str, object],
) -> None:
    """An unusable audit is lock_unknown even when both reads are identical."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    runner = FakeHelperRunner(inventory_outputs=(half, half), **audit)
    response = _health(runner)
    assert response["evidence"]["dpkg"] == "lock_unknown"


def test_check_health_audit_non_utf8_output_is_lock_unknown() -> None:
    """Non-UTF8 dpkg --audit output cannot prove the lock is clear either."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    runner = FakeHelperRunner(
        inventory_outputs=(half, half), audit_stdout_bytes=b"\xff\xfe not utf-8"
    )
    response = _health(runner)
    assert response["ok"] is True
    assert response["evidence"]["dpkg"] == "lock_unknown"


def test_check_health_audit_output_bound_is_tight_and_becomes_lock_unknown() -> None:
    """Oversized dpkg --audit output is bounded and reported as lock_unknown."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"

    def oversized_audit_runner(argv, timeout, max_output):
        if "dpkg --audit" in " ".join(argv):
            assert max_output == helper.HEALTH_AUDIT_MAX_BYTES
            return helper.CommandResult(0, b"", b"", output_exceeded=True)
        return FakeHelperRunner(inventory_outputs=(half,))(argv, timeout, max_output)

    response = _health(oversized_audit_runner)
    assert response["evidence"]["dpkg"] == "lock_unknown"


def test_check_health_malformed_inventory_is_bounded_and_semantic() -> None:
    """Unparseable dpkg-query output is Health's own malformed_evidence."""
    runner = FakeHelperRunner(inventory_outputs=("not\ta\tvalid\tline\textra\n",))
    response = _health(runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "malformed_evidence"


def test_check_health_unsupported_dpkg_is_bounded_and_semantic() -> None:
    """A guest without dpkg-query is unsupported_guest, not malformed."""

    def no_dpkg_runner(argv, timeout, max_output):
        if "dpkg-query" in " ".join(argv):
            return helper.CommandResult(127, b"", b"command not found\n")
        return FakeHelperRunner()(argv, timeout, max_output)

    response = _health(no_dpkg_runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "unsupported_guest"


@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, None)])
def test_check_health_preserves_reboot_tri_state(
    returncode: int, expected: bool | None
) -> None:
    """Only the marker's presence is reliable; absence is never ``false``."""
    response = _health(FakeHelperRunner(reboot_returncode=returncode))
    assert response["evidence"]["dpkg"] == "ok"
    assert response["evidence"]["reboot_required"] is expected


@pytest.mark.parametrize("returncode", [2, 126, 127, 255])
def test_check_health_reboot_probe_error_while_running_is_not_healthy_evidence(
    returncode: int,
) -> None:
    """``test -e`` exits 1 for an absent marker; any other failure is no evidence.

    Collapsing a failed probe into ``reboot_required: null`` would let a
    clean dpkg state classify as HEALTHY, so it is a bounded helper failure
    (UNKNOWN in Home Assistant) instead.
    """
    runner = FakeHelperRunner(reboot_returncode=returncode)
    response = _health(runner)
    assert response["ok"] is False
    assert response["error"] == {
        "classification": "execution_failed",
        "message": "reboot-required probe failed",
    }
    assert "evidence" not in response
    # The guest was re-confirmed running before this was treated as a failure.
    assert runner.status_calls == 2


def test_check_health_reboot_probe_error_after_guest_gone_is_still_unavailable() -> None:
    """A non-1 reboot probe result from a vanished guest keeps its existing shape."""
    runner = FakeHelperRunner(reboot_returncode=255)

    def gone_at_reboot_runner(argv, timeout, max_output):
        if "/var/run/reboot-required" in " ".join(argv):
            runner.status = "stopped"
            return helper.CommandResult(255, b"", b"")
        return runner(argv, timeout, max_output)

    response = _health(gone_at_reboot_runner)
    assert response["ok"] is True
    assert response["evidence"]["guest_exec"] is False
    assert response["evidence"]["guest_exec_unavailable"] is True


def test_check_health_reboot_probe_guest_gone_is_unknown_not_stale() -> None:
    """A guest that disappears right at the reboot probe discards stale evidence.

    A nonzero (not just rc==1) reboot-marker result is re-checked against
    native LXC status; when the guest is no longer confirmed running, the
    whole result becomes one unestablished UNKNOWN rather than a mix of
    real dpkg evidence and a reboot marker that could not be trusted.
    """
    runner = FakeHelperRunner(reboot_returncode=1)

    def gone_at_reboot_runner(argv, timeout, max_output):
        if "/var/run/reboot-required" in " ".join(argv):
            runner.status = "stopped"
            return helper.CommandResult(1, b"", b"")
        return runner(argv, timeout, max_output)

    response = _health(gone_at_reboot_runner)
    assert response["ok"] is True
    assert response["evidence"] == {
        "guest_exec": False,
        "guest_exec_unavailable": True,
        "dpkg": None,
        "unfinished_package_count": None,
        "reboot_required": None,
    }


def test_check_health_reboot_marker_timeout_is_bounded_unknown() -> None:
    """A hanging reboot-marker check is a bounded timeout, never a hang."""

    def timeout_runner(argv, timeout, max_output):
        if "/var/run/reboot-required" in " ".join(argv):
            return helper.CommandResult(-9, b"", b"", timed_out=True)
        return FakeHelperRunner()(argv, timeout, max_output)

    response = _health(timeout_runner)
    assert response["ok"] is False
    assert response["error"]["classification"] == "timeout"


@pytest.mark.parametrize(
    ("dpkg_status", "reboot", "gone_at_reboot", "expected"),
    [
        # Established interrupted dpkg is FAILED whatever the reboot probe does
        # while the guest stays confirmed running.
        ("half-installed", 2, False, HealthState.FAILED),
        ("half-installed", 127, False, HealthState.FAILED),
        ("half-installed", "timeout", False, HealthState.FAILED),
        ("half-installed", 1, False, HealthState.FAILED),
        ("half-installed", 0, False, HealthState.FAILED),
        # Without FAILED evidence a failed probe stays fail-closed UNKNOWN.
        ("installed", 2, False, None),
        ("installed", "timeout", False, None),
        # A guest gone at the reboot probe still discards everything.
        ("half-installed", 255, True, None),
        ("half-installed", "timeout", True, None),
    ],
)
def test_check_health_reboot_probe_failure_never_erases_interrupted_dpkg(
    dpkg_status: str, reboot: int | str, gone_at_reboot: bool, expected
) -> None:
    """Reboot uncertainty blocks HEALTHY/DEGRADED but never an established FAILED."""
    line = f"openssl\tamd64\t3.0.11-1\t{dpkg_status}\n"
    runner = FakeHelperRunner(
        inventory_outputs=(line, line), audit_stdout=AUDIT_HALF_INSTALLED
    )

    def reboot_runner(argv, timeout, max_output):
        if "/var/run/reboot-required" in " ".join(argv):
            if gone_at_reboot:
                runner.status = "stopped"
            if reboot == "timeout":
                return helper.CommandResult(-9, b"", b"", timed_out=True)
            return helper.CommandResult(reboot, b"", b"")
        return runner(argv, timeout, max_output)

    response = _health(reboot_runner)
    if response["ok"] is not True:
        assert expected is None
        return
    outcome = classify_health(_parse_health_response(response, "pve1", 200))
    assert outcome.state is expected
    if expected is HealthState.FAILED:
        assert outcome.reason is HealthReason.DPKG_INTERRUPTED
        assert response["evidence"]["reboot_required"] is (
            True if reboot == 0 else None
        )


def test_check_health_has_its_own_tight_timeout_model() -> None:
    """Health does not reuse scan-sized bounds; it has its own tight model."""
    assert helper.HEALTH_OPERATION_TIMEOUT_SECONDS == 60.0
    assert helper.HEALTH_COMMAND_TIMEOUT_SECONDS == 20.0
    assert helper.PING_COMMAND_TIMEOUT_SECONDS == 10.0
    assert helper.HEALTH_OPERATION_TIMEOUT_SECONDS < helper.OPERATION_TIMEOUT_SECONDS
    assert helper.HEALTH_COMMAND_TIMEOUT_SECONDS < helper.COMMAND_TIMEOUT_SECONDS
    # Scan/Update/Autoremove timeout semantics are unchanged.
    assert helper.OPERATION_TIMEOUT_SECONDS == 240.0
    assert helper.COMMAND_TIMEOUT_SECONDS == 120.0
    assert helper.UPDATE_OPERATION_TIMEOUT_SECONDS == 1800.0
    assert helper.UPDATE_COMMAND_TIMEOUT_SECONDS == 1500.0


def test_check_health_guest_commands_use_the_health_command_bound() -> None:
    """Health's own guest commands are capped at 20s, not the generic 120s."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    runner = FakeHelperRunner(inventory_outputs=(half, half), reboot_returncode=0)
    _health(runner)
    guest_commands = [
        call
        for call in runner.calls
        if call[0][:3] == ("pct", "exec", "200")
        and call[0][4:5] != ("/bin/true",)
    ]
    dpkg_and_reboot_calls = [
        call
        for call in guest_commands
        if "dpkg-query" in " ".join(call[0])
        or "dpkg --audit" in " ".join(call[0])
        or "/var/run/reboot-required" in " ".join(call[0])
    ]
    assert dpkg_and_reboot_calls
    assert all(
        call[1] == helper.HEALTH_COMMAND_TIMEOUT_SECONDS
        for call in dpkg_and_reboot_calls
    )
    exec_call = next(
        call for call in runner.calls if call[0][-1:] == ("/bin/true",)
    )
    assert exec_call[1] == helper.PING_COMMAND_TIMEOUT_SECONDS


def test_check_health_deadline_exceeded_is_a_bounded_timeout() -> None:
    """Health shares the generic bounded-deadline mechanism, now at 60s."""

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
        clock.value += 100.0
        return result

    response = helper.handle_request(
        _request(operation="check_health"),
        runner=advancing_runner,
        clock=clock,
        lock_factory=lambda _vmid: nullcontext(),
    )
    assert response["error"] == {
        "classification": "timeout",
        "message": "package scan operation deadline exceeded",
    }
    # The very first command already receives no more than the 60s Health
    # deadline, unlike the 120s generic command cap.
    assert timeouts[0] == 60.0


def test_check_health_evidence_never_carries_package_names_or_guest_text() -> None:
    """Health evidence stays limited to bounded enums/bools/ints only."""
    half = "openssl\tamd64\t3.0.11-1\thalf-installed\n"
    runner = FakeHelperRunner(
        inventory_outputs=(half, half),
        audit_stdout="openssl: some diagnostic line\n",
    )
    response = _health(runner)
    assert set(response["evidence"]) == {
        "guest_exec",
        "guest_exec_unavailable",
        "dpkg",
        "unfinished_package_count",
        "reboot_required",
    }
    assert "openssl" not in json.dumps(response["evidence"])
