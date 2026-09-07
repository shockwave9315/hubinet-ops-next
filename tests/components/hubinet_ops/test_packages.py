"""Tests for the bounded LXC package scan."""

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

from custom_components.hubinet_ops.packages.models import (
    PackageScanError,
    PackageScanFailure,
    PackageScanResult,
    PendingPackage,
)
from custom_components.hubinet_ops.packages.parser import (
    PackageScanParseError,
    parse_apt_simulation,
    parse_installed_inventory,
    parse_native_architecture,
    parse_os_release,
)
from custom_components.hubinet_ops.packages.scanner import PackageScanner, ProcessResult
import pytest

from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from tests.common import MockConfigEntry  # noqa: TID251

from . import setup_integration  # noqa: TID251

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER_PATH = REPO_ROOT / "deploy/hubinet-package-scan-helper.py"

ZERO_SIMULATION = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
TWO_UPDATES = """\
Inst openssl [3.0.11-1] (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Inst apt [2.6.1] (2.6.2 Debian:12/oldstable [amd64])
Conf openssl (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Conf apt (2.6.2 Debian:12/oldstable [amd64])
2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('ID=debian\nVERSION_ID="12"\n', ("debian", "12")),
        ('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n', ("ubuntu", "24.04")),
    ],
)
def test_parse_supported_os_release(text: str, expected: tuple[str, str]) -> None:
    """Debian and Ubuntu os-release evidence is accepted."""
    assert parse_os_release(text) == expected


def test_unsupported_guest_os_fails_closed() -> None:
    """An unsupported distribution has a bounded failure class."""
    with pytest.raises(PackageScanError) as caught:
        parse_os_release('ID=alpine\nVERSION_ID="3.20"\n')
    assert caught.value.failure is PackageScanFailure.UNSUPPORTED_OS


def test_zero_single_and_multiple_updates() -> None:
    """Exact empty, single, and multiple upgrade plans are parsed."""
    assert (
        parse_apt_simulation(
            ZERO_SIMULATION,
            native_architecture="amd64\n",
            installed_inventory="",
        )
        == ()
    )
    single = parse_apt_simulation(
        "Inst apt [2.6.1] (2.6.2 Debian:12/oldstable [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        native_architecture="amd64",
        installed_inventory="apt\tamd64\t2.6.1\tinstalled\n",
    )
    assert [(package.name, package.security) for package in single] == [("apt", False)]
    multiple = parse_apt_simulation(
        TWO_UPDATES,
        native_architecture="amd64",
        installed_inventory=TWO_INVENTORY,
    )
    assert [(package.name, package.security) for package in multiple] == [
        ("apt", False),
        ("openssl", True),
    ]


def test_multiarch_packages_remain_distinct() -> None:
    """Native and foreign package identities are not collapsed."""
    result = parse_apt_simulation(
        "Inst libc6 [2.31-1] (2.31-2 Debian:stable [amd64])\n"
        "Inst libc6:i386 [2.31-1] (2.31-2 Debian:stable [i386])\n"
        "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        native_architecture="amd64",
        installed_inventory=(
            "libc6\tamd64\t2.31-1\tinstalled\nlibc6\ti386\t2.31-1\tinstalled\n"
        ),
    )
    assert {(package.name, package.architecture) for package in result} == {
        ("libc6", "amd64"),
        ("libc6", "i386"),
    }


@pytest.mark.parametrize(
    "simulation",
    [
        "Reading package lists...\n",
        "Inst apt broken\n1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst apt [2.6.1] (2.6.2 Debian:12 [amd64])\n0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Remv apt [2.6.1]\n0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
    ],
)
def test_malformed_apt_simulation_fails_closed(simulation: str) -> None:
    """Malformed or non-upgrade-only simulations never become package results."""
    with pytest.raises(PackageScanParseError):
        parse_apt_simulation(
            simulation,
            native_architecture="amd64",
            installed_inventory="apt\tamd64\t2.6.1\tinstalled\n",
        )


def test_unfinished_dpkg_state_fails_closed() -> None:
    """Both dpkg inventory and APT bad-count evidence fail closed."""
    inventory = parse_installed_inventory("foo\tamd64\t1.0\thalf-configured\n")
    assert inventory.unfinished == (("foo", "amd64", "half-configured"),)
    with pytest.raises(PackageScanParseError, match="unfinished"):
        parse_apt_simulation(
            ZERO_SIMULATION,
            native_architecture="amd64",
            installed_inventory="foo\tamd64\t1.0\thalf-configured\n",
        )
    with pytest.raises(PackageScanParseError, match="unfinished"):
        parse_apt_simulation(
            f"{ZERO_SIMULATION}1 not fully installed or removed.\n",
            native_architecture="amd64",
            installed_inventory="",
        )


@pytest.mark.parametrize("text", ["all\n", "", "amd64\ni386\n", "AMD64\n"])
def test_native_architecture_requires_one_real_dpkg_architecture(text: str) -> None:
    """Native architecture parsing rejects ambiguous and malformed evidence."""
    with pytest.raises(PackageScanParseError):
        parse_native_architecture(text)


class FakeHelperRunner:
    """Return deterministic evidence for the helper's fixed command sequence."""

    def __init__(
        self,
        *,
        os_release: str = 'ID=debian\nVERSION_ID="12"\n',
        update_returncode: int = 0,
        update_stderr: str = "",
        reboot_required: bool = False,
    ) -> None:
        """Initialize fixed helper outcomes."""
        self.os_release = os_release
        self.update_returncode = update_returncode
        self.update_stderr = update_stderr
        self.reboot_required = reboot_required
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, _timeout, _max_output):
        """Return evidence for one fixed argv shape."""
        self.calls.append(argv)
        rendered = " ".join(argv)
        if "/etc/os-release" in rendered:
            return helper.CommandResult(0, self.os_release.encode(), b"")
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
            return helper.CommandResult(0 if self.reboot_required else 1, b"", b"")
        raise AssertionError(f"unexpected command: {argv!r}")


def _request(vmid: int = 200) -> dict[str, object]:
    return {
        "request_version": 1,
        "operation": "scan_packages",
        "target": {"vmid": vmid},
    }


def test_helper_uses_only_fixed_pct_argv_and_reports_reboot() -> None:
    """The helper uses fixed commands without a shell and returns reboot evidence."""
    runner = FakeHelperRunner(reboot_required=True)
    response = helper.handle_request(_request(), runner=runner)
    assert response["ok"] is True
    assert response["evidence"]["reboot_required"] is True
    assert all(call[:4] == ("pct", "exec", "200", "--") for call in runner.calls)
    assert any(
        call[-4:] == ("apt-get", "update", "-qq", "--error-on=any")
        for call in runner.calls
    )
    assert any(call[-3:] == ("apt-get", "-s", "upgrade") for call in runner.calls)


def test_helper_classifies_busy_lock_and_stops() -> None:
    """A busy dpkg lock is bounded and prevents simulation."""
    runner = FakeHelperRunner(
        update_returncode=100,
        update_stderr="E: Could not get lock /var/lib/dpkg/lock-frontend",
    )
    response = helper.handle_request(_request(), runner=runner)
    assert response["error"]["classification"] == "package_manager_busy"
    assert not any("upgrade" in call for call in runner.calls)


def test_helper_classifies_command_timeout() -> None:
    """A timed-out fixed guest command returns a bounded timeout failure."""
    response = helper.handle_request(
        _request(),
        runner=lambda _argv, _timeout, _max_output: helper.CommandResult(
            -9, b"", b"", timed_out=True
        ),
    )
    assert response["error"] == {
        "classification": "timeout",
        "message": "package scan command timed out",
    }


def test_helper_rejects_arbitrary_operations_and_vmids() -> None:
    """The forced command accepts no command text or free-form VMID."""
    with pytest.raises(helper.RequestError):
        helper.validate_request({**_request(), "operation": "upgrade_packages"})
    with pytest.raises(helper.RequestError):
        helper.validate_request(_request(vmid=True))


def test_scanner_pins_ssh_trust_and_parses_response(tmp_path: Path) -> None:
    """The client sends a fixed SSH invocation and parses helper evidence."""
    calls = []

    def runner(argv, request, timeout, max_output):
        calls.append((argv, json.loads(request), timeout, max_output))
        payload = {
            "response_version": 1,
            "ok": True,
            "target": {"vmid": 200},
            "evidence": {
                "os_release": 'ID=debian\nVERSION_ID="12"\n',
                "native_architecture": "amd64\n",
                "installed_inventory": TWO_INVENTORY,
                "simulation": TWO_UPDATES,
                "reboot_required": False,
            },
        }
        return ProcessResult(0, json.dumps(payload).encode(), b"")

    scanner = PackageScanner(
        private_key_path=tmp_path / "key",
        known_hosts_path=tmp_path / "known_hosts",
        runner=runner,
    )
    result = scanner.scan("pve1", 200)
    assert len(result.packages) == 2
    argv, request, timeout, max_output = calls[0]
    assert argv[0] == "ssh"
    assert argv[-1] == "root@pve1"
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert request == _request()
    assert timeout > 0
    assert max_output > 0


def test_scanner_classifies_bounded_transport_failures(tmp_path: Path) -> None:
    """The outer SSH boundary classifies timeout and output overflow."""
    for process_result, expected_failure in [
        (ProcessResult(-9, b"", b"", timed_out=True), PackageScanFailure.TIMEOUT),
        (
            ProcessResult(-9, b"", b"", output_exceeded=True),
            PackageScanFailure.EXECUTION_FAILED,
        ),
    ]:

        def runner(_argv, _request, _timeout, _max_output, result=process_result):
            return result

        scanner = PackageScanner(
            private_key_path=tmp_path / "key",
            known_hosts_path=tmp_path / "known_hosts",
            runner=runner,
        )
        with pytest.raises(PackageScanError) as caught:
            scanner.scan("pve1", 200)
        assert caught.value.failure is expected_failure


async def test_package_scan_uses_known_lxc_and_updates_sensor(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The button scans the coordinator's LXC identity and publishes its result."""
    with patch(
        "custom_components.hubinet_ops.PLATFORMS",
        [Platform.BUTTON, Platform.SENSOR],
    ):
        await setup_integration(hass, mock_config_entry)

    result = PackageScanResult(
        os_id="debian",
        os_version="12",
        packages=(
            PendingPackage("openssl", "amd64", "1.0", "1.1", "Debian-Security", True),
        ),
        reboot_required=True,
    )
    coordinator = mock_config_entry.runtime_data
    coordinator.package_scanner.scan = MagicMock(return_value=result)

    with pytest.raises(PackageScanError) as missing:
        await coordinator.async_scan_packages("pve1", 999)
    assert missing.value.failure is PackageScanFailure.GUEST_UNAVAILABLE
    with pytest.raises(PackageScanError) as stopped:
        await coordinator.async_scan_packages("pve1", 201)
    assert stopped.value.failure is PackageScanFailure.GUEST_UNAVAILABLE
    coordinator.package_scanner.scan.assert_not_called()

    await hass.services.async_call(
        "button",
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.ct_nginx_scan_pending_packages"},
        blocking=True,
    )

    coordinator.package_scanner.scan.assert_called_once_with("pve1", 200)
    state = hass.states.get("sensor.ct_nginx_pending_package_updates")
    assert state is not None
    assert state.state == "1"
    assert state.attributes["security_updates"] == 1
    assert state.attributes["reboot_required"] is True
