"""Static and parsable checks for the finite PVE bootstrap."""

import hashlib
import json
from pathlib import Path
import re
import subprocess

import custom_components.hubinet_ops
from custom_components.hubinet_ops.const import INTEGRATION_VERSION

REPOSITORY_ROOT = Path(custom_components.hubinet_ops.__file__).parents[2]
BOOTSTRAP = REPOSITORY_ROOT / "deploy/bootstrap-proxmox.sh"
HELPER = REPOSITORY_ROOT / "deploy/hubinet-package-scan-helper.py"
MANIFEST = REPOSITORY_ROOT / "custom_components/hubinet_ops/manifest.json"


def _source() -> str:
    return BOOTSTRAP.read_text()


def test_bootstrap_has_valid_shell_syntax_and_bounded_options() -> None:
    """The script parses and handles help/unknown options before host access."""
    subprocess.run(["sh", "-n", BOOTSTRAP], check=True)
    help_result = subprocess.run(
        ["sh", BOOTSTRAP, "--help"], check=False, capture_output=True, text=True
    )
    assert help_result.returncode == 0
    assert "--reset" in help_result.stdout
    bad_result = subprocess.run(
        ["sh", BOOTSTRAP, "--everything"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert bad_result.returncode == 2


def test_release_version_and_helper_hash_are_consistent() -> None:
    """Manifest, bootstrap tag, and exact helper bytes remain locked together."""
    source = _source()
    version = json.loads(MANIFEST.read_text())["version"]
    assert version == INTEGRATION_VERSION
    assert f'RELEASE_VERSION="{version}"' in source
    assert f"/${{RELEASE_VERSION}}/deploy/" in source
    expected_hash = re.search(r'HELPER_SHA256="([0-9a-f]{64})"', source)
    assert expected_hash is not None
    assert expected_hash.group(1) == hashlib.sha256(HELPER.read_bytes()).hexdigest()


def test_bootstrap_owns_only_the_fixed_resource_contract() -> None:
    """Names, privileges, helper target, and authorized-key file stay finite."""
    source = _source()
    for value in (
        'PVE_USER="hubinetnext@pve"',
        'PVE_ROLE="HubinetOpsNext"',
        'PVE_TOKEN_ID="ha"',
        'AUTHORIZED_KEYS2="/root/.ssh/authorized_keys2"',
        'HELPER_TARGET="/usr/local/sbin/hubinet-package-scan-helper"',
        "Datastore.Audit Sys.Audit Sys.PowerMgmt VM.Audit VM.PowerMgmt VM.Snapshot",
    ):
        assert value in source
    assert "/root/.ssh/authorized_keys\"" not in source
    assert "/etc/pve/priv/authorized_keys" not in source
    assert "sshd_config" not in source


def test_token_collision_guard_precedes_every_provisioning_write() -> None:
    """Normal existing-token recovery exits before deterministic mutations."""
    source = _source()
    guard = source.index('if [ "$TOKEN_EXISTS" -eq 1 ] && [ "$RESET" -eq 0 ]')
    assert guard < source.index("pveum role add")
    assert guard < source.index("pveum user add")
    assert guard < source.index("pveum acl modify")
    assert "bash -s -- --reset" in source
    assert source.index("pveum user token add") > source.index("install -o root")
