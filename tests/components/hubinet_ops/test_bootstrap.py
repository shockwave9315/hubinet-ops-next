"""Behavioral tests for the finite PVE bootstrap."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import asyncssh
import pytest

import custom_components.hubinet_ops
from custom_components.hubinet_ops.const import INTEGRATION_VERSION
from custom_components.hubinet_ops.enrollment import parse_enrollment

REPOSITORY_ROOT = Path(custom_components.hubinet_ops.__file__).parents[2]
BOOTSTRAP = REPOSITORY_ROOT / "deploy/bootstrap-proxmox.sh"
HELPER = REPOSITORY_ROOT / "deploy/hubinet-package-scan-helper.py"
MANIFEST = REPOSITORY_ROOT / "custom_components/hubinet_ops/manifest.json"
PRIVILEGES = (
    "Datastore.Audit Sys.Audit Sys.PowerMgmt VM.Audit VM.PowerMgmt VM.Snapshot"
)
AUTHORIZED_LINE = re.compile(
    rb'^restrict,command="/usr/local/sbin/hubinet-package-scan-helper" '
    rb"ssh-ed25519 [A-Za-z0-9+/]+={0,3} hubinet-ops\n$"
)


def _source() -> str:
    return BOOTSTRAP.read_text()


@dataclass
class BootstrapHarness:
    """Disposable product filesystem and fake PVE command state."""

    root: Path
    fake_bin: Path
    state_path: Path
    env: dict[str, str]

    @property
    def authorized_keys2(self) -> Path:
        return self.root / "root/.ssh/authorized_keys2"

    @property
    def helper(self) -> Path:
        return self.root / "usr/local/sbin/hubinet-package-scan-helper"

    def run(self, *args: str, script: Path = BOOTSTRAP) -> subprocess.CompletedProcess:
        """Run bootstrap against the disposable PVE boundary."""
        return subprocess.run(
            ["/bin/sh", script, *args],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def state(self) -> dict[str, object]:
        """Read fake pveum state."""
        return json.loads(self.state_path.read_text())

    def write_state(self, state: dict[str, object]) -> None:
        """Replace fake pveum state."""
        self.state_path.write_text(json.dumps(state))

    def clear_calls(self) -> None:
        """Clear only the fake pveum call log."""
        state = self.state()
        state["calls"] = []
        self.write_state(state)

    def enroll(self) -> tuple[subprocess.CompletedProcess, str]:
        """Perform one successful fresh enrollment and return its value."""
        result = self.run()
        assert result.returncode == 0, result.stderr
        values = re.findall(r"HUBINET1-[A-Za-z0-9_-]+", result.stdout)
        assert len(values) == 1
        return result, values[0]


def _write_executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip())
    path.chmod(0o755)


def _new_harness(tmp_path: Path) -> BootstrapHarness:
    root = tmp_path / "pve-root"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (root / "etc/pve/local").mkdir(parents=True)
    (root / "etc/ssh").mkdir(parents=True)
    (root / "root").mkdir(parents=True)
    (root / "usr/local/sbin").mkdir(parents=True)

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    (root / "etc/ssh/ssh_host_ed25519_key.pub").write_bytes(
        host_key.export_public_key()
    )
    state_path = tmp_path / "pveum-state.json"
    state_path.write_text(
        json.dumps(
            {
                "roles": [],
                "users": [],
                "acls": [],
                "tokens": {},
                "token_counter": 0,
                "fail_token_add": False,
                "calls": [],
            }
        )
    )

    _write_executable(
        fake_bin / "pveum",
        r"""
        #!/usr/bin/env python3
        import json
        import os
        import sys

        path = os.environ["PVEUM_STATE"]
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        args = sys.argv[1:]
        state["calls"].append(" ".join(args))

        def save():
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(state, handle)

        if args[:2] == ["role", "list"]:
            print(json.dumps(state["roles"]))
        elif args[:2] in (["role", "add"], ["role", "modify"]):
            role = next(
                (row for row in state["roles"] if row["roleid"] == args[2]),
                None,
            )
            if role is None:
                role = {"roleid": args[2]}
                state["roles"].append(role)
            role["privs"] = args[args.index("--privs") + 1]
        elif args[:2] == ["user", "list"]:
            print(json.dumps(state["users"]))
        elif args[:2] in (["user", "add"], ["user", "modify"]):
            user = next(
                (row for row in state["users"] if row["userid"] == args[2]),
                None,
            )
            if user is None:
                user = {"userid": args[2]}
                state["users"].append(user)
            user.update({"enable": 1, "expire": 0})
        elif args[:2] == ["acl", "list"]:
            print(json.dumps(state["acls"]))
        elif args[:2] == ["acl", "modify"]:
            wanted = {
                "path": args[2],
                "ugid": args[args.index("--users") + 1],
                "roleid": args[args.index("--roles") + 1],
                "propagate": int(args[args.index("--propagate") + 1]),
            }
            if not any(row == wanted for row in state["acls"]):
                state["acls"].append(wanted)
        elif args[:3] == ["user", "token", "list"]:
            prefix = f"{args[3]}!"
            print(json.dumps([
                {"tokenid": key.removeprefix(prefix)}
                for key in state["tokens"] if key.startswith(prefix)
            ]))
        elif args[:3] == ["user", "token", "remove"]:
            state["tokens"].pop(f"{args[3]}!{args[4]}", None)
        elif args[:3] == ["user", "token", "add"]:
            if state["fail_token_add"]:
                save()
                raise SystemExit(1)
            state["token_counter"] += 1
            value = f"token-secret-{state['token_counter']}"
            state["tokens"][f"{args[3]}!{args[4]}"] = value
            print(json.dumps({"value": value}))
        else:
            save()
            raise SystemExit(f"unexpected pveum arguments: {args}")
        save()
        """,
    )
    _write_executable(
        fake_bin / "python3",
        r"""
        #!/bin/sh
        if [ "${1-}" = "${FAKE_HELPER_TARGET-}" ]; then
          printf '%s\n' '{"protocol_version":1,"helper_version":2,"operation":"probe","ok":true,"node":"pve1"}'
          exit 0
        fi
        exec /usr/bin/python3 "$@"
        """,
    )
    _write_executable(
        fake_bin / "curl",
        r"""
        #!/bin/sh
        output=""
        while [ "$#" -gt 0 ]; do
          if [ "$1" = "-o" ]; then
            output=$2
            shift 2
          else
            shift
          fi
        done
        [ -n "$output" ] || exit 2
        /bin/cp "$FAKE_HELPER_SOURCE" "$output"
        """,
    )
    _write_executable(
        fake_bin / "install",
        r"""
        #!/bin/sh
        directory=0
        mode=""
        while [ "$#" -gt 0 ]; do
          case "$1" in
            -d) directory=1; shift ;;
            -o|-g) shift 2 ;;
            -m) mode=$2; shift 2 ;;
            *) break ;;
          esac
        done
        if [ "$directory" -eq 1 ]; then
          /bin/mkdir -p "$1"
          /bin/chmod "$mode" "$1"
        else
          /bin/cp "$1" "$2"
          /bin/chmod "$mode" "$2"
        fi
        """,
    )
    _write_executable(fake_bin / "id", '#!/bin/sh\nprintf "0\\n"\n')
    _write_executable(fake_bin / "pveversion", "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "pvesh", '#!/bin/sh\nprintf "{}\\n"\n')
    _write_executable(fake_bin / "pct", "#!/bin/sh\nexit 0\n")
    _write_executable(
        fake_bin / "sshd",
        '#!/bin/sh\nprintf "%s\\n" "authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2"\n',
    )
    _write_executable(
        fake_bin / "readlink",
        '#!/bin/sh\nprintf "%s\\n" "/etc/pve/nodes/pve1"\n',
    )
    _write_executable(fake_bin / "stat", '#!/bin/sh\nprintf "0:0:755\\n"\n')

    for tool in ("awk", "mktemp", "mv", "rm", "sha256sum", "ssh-keygen"):
        target = shutil.which(tool)
        assert target is not None
        (fake_bin / tool).symlink_to(target)

    env = {
        **os.environ,
        "PATH": str(fake_bin),
        "PVEUM_STATE": str(state_path),
        "FAKE_HELPER_SOURCE": str(HELPER),
        "FAKE_HELPER_TARGET": str(
            root / "usr/local/sbin/hubinet-package-scan-helper"
        ),
        "_HUBINET_BOOTSTRAP_TESTING": "1",
        "_HUBINET_BOOTSTRAP_TEST_ROOT": str(root),
    }
    return BootstrapHarness(root, fake_bin, state_path, env)


def _credential_calls(state: dict[str, object]) -> list[str]:
    return [
        call
        for call in state["calls"]
        if call.startswith("user token add") or call.startswith("user token remove")
    ]


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
    assert _source().rstrip().endswith('main "$@"')


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
        PRIVILEGES,
    ):
        assert value in source
    assert "/root/.ssh/authorized_keys\"" not in source
    assert "/etc/pve/priv/authorized_keys" not in source
    assert "sshd_config" not in source


def test_fresh_install_provisions_exact_resources_and_one_enrollment(
    tmp_path: Path,
) -> None:
    """B1: an empty PVE becomes a complete, parseable guided enrollment."""
    harness = _new_harness(tmp_path)
    result, value = harness.enroll()
    state = harness.state()

    assert state["roles"] == [{"roleid": "HubinetOpsNext", "privs": PRIVILEGES}]
    assert state["users"] == [
        {"userid": "hubinetnext@pve", "enable": 1, "expire": 0}
    ]
    assert {tuple(row.values()) for row in state["acls"]} == {
        ("/", "hubinetnext@pve", "HubinetOpsNext", 1)
    }
    assert state["tokens"] == {"hubinetnext@pve!ha": "token-secret-1"}
    assert harness.helper.read_bytes() == HELPER.read_bytes()
    assert AUTHORIZED_LINE.fullmatch(harness.authorized_keys2.read_bytes())
    enrollment = parse_enrollment(value)
    assert enrollment.token_secret == "token-secret-1"
    assert enrollment.host_key.startswith("ssh-ed25519 ")
    assert "PRIVATE KEY" in enrollment.private_key
    assert result.stdout.count("HUBINET1-") == 1


def test_plain_enrolled_rerun_preserves_credentials(tmp_path: Path) -> None:
    """B2: plain repair never rotates or re-emits valid credentials."""
    harness = _new_harness(tmp_path)
    harness.enroll()
    before_key = harness.authorized_keys2.read_bytes()
    harness.authorized_keys2.parent.chmod(0o711)
    before_tokens = dict(harness.state()["tokens"])
    harness.clear_calls()

    result = harness.run()

    assert result.returncode == 0
    assert "HUBINET1-" not in result.stdout
    assert "credentials were unchanged" in result.stderr
    assert harness.authorized_keys2.read_bytes() == before_key
    assert harness.authorized_keys2.parent.stat().st_mode & 0o777 == 0o711
    assert harness.state()["tokens"] == before_tokens
    assert _credential_calls(harness.state()) == []


def test_plain_rerun_repairs_helper_without_rotating(tmp_path: Path) -> None:
    """B3: helper repair is deterministic and independent of credentials."""
    harness = _new_harness(tmp_path)
    harness.enroll()
    before_key = harness.authorized_keys2.read_bytes()
    harness.helper.write_text("broken helper")
    harness.clear_calls()

    result = harness.run()

    assert result.returncode == 0
    assert harness.helper.read_bytes() == HELPER.read_bytes()
    assert harness.authorized_keys2.read_bytes() == before_key
    assert _credential_calls(harness.state()) == []
    assert "HUBINET1-" not in result.stdout


def test_plain_rerun_repairs_role_without_rotating(tmp_path: Path) -> None:
    """B4: the role is restored to exactly the accepted six privileges."""
    harness = _new_harness(tmp_path)
    harness.enroll()
    state = harness.state()
    state["roles"][0]["privs"] = "Sys.Audit VM.Audit"
    harness.write_state(state)
    before_key = harness.authorized_keys2.read_bytes()
    harness.clear_calls()

    result = harness.run()

    assert result.returncode == 0
    assert harness.state()["roles"][0]["privs"] == PRIVILEGES
    assert harness.authorized_keys2.read_bytes() == before_key
    assert _credential_calls(harness.state()) == []


def test_explicit_reset_rotates_both_credentials_and_emits_enrollment(
    tmp_path: Path,
) -> None:
    """B5: --reset replaces the Hubinet SSH key and fixed API token."""
    harness = _new_harness(tmp_path)
    _, old_value = harness.enroll()
    old_key = harness.authorized_keys2.read_bytes()
    old_enrollment = parse_enrollment(old_value)
    harness.clear_calls()

    result = harness.run("--reset")

    assert result.returncode == 0
    values = re.findall(r"HUBINET1-[A-Za-z0-9_-]+", result.stdout)
    assert len(values) == 1
    new_enrollment = parse_enrollment(values[0])
    assert new_enrollment.token_secret != old_enrollment.token_secret
    assert new_enrollment.private_key != old_enrollment.private_key
    assert harness.authorized_keys2.read_bytes() != old_key
    assert _credential_calls(harness.state()) == [
        "user token remove hubinetnext@pve ha",
        "user token add hubinetnext@pve ha --privsep 0 --output-format json",
    ]


@pytest.mark.parametrize(
    "broken_contents",
    [None, b"restrict ssh-ed25519 malformed hubinet-ops\n"],
    ids=["missing", "malformed"],
)
def test_broken_ssh_state_requires_reset_after_deterministic_repair(
    tmp_path: Path, broken_contents: bytes | None
) -> None:
    """B6: an unrecoverable client key is reported without silent rotation."""
    harness = _new_harness(tmp_path)
    harness.enroll()
    if broken_contents is None:
        harness.authorized_keys2.unlink()
    else:
        harness.authorized_keys2.write_bytes(broken_contents)
    harness.helper.write_text("broken helper")
    state = harness.state()
    state["roles"][0]["privs"] = "Sys.Audit"
    harness.write_state(state)
    harness.clear_calls()

    result = harness.run()

    assert result.returncode != 0
    assert harness.helper.read_bytes() == HELPER.read_bytes()
    assert harness.state()["roles"][0]["privs"] == PRIVILEGES
    if broken_contents is None:
        assert harness.authorized_keys2.exists() is False
    else:
        assert harness.authorized_keys2.read_bytes() == broken_contents
    assert _credential_calls(harness.state()) == []
    assert "bash -s -- --reset" in result.stderr
    assert "Reconfigure → Re-enroll" in result.stderr
    assert "HUBINET1-" not in result.stdout


def test_foreign_authorized_keys2_fails_before_any_mutation(tmp_path: Path) -> None:
    """B7: the release-exclusive file is never merged or rewritten."""
    harness = _new_harness(tmp_path)
    foreign = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest foreign@example\n"
    harness.authorized_keys2.parent.mkdir(parents=True)
    harness.authorized_keys2.write_bytes(foreign)
    before = harness.state()

    result = harness.run()

    assert result.returncode != 0
    assert harness.authorized_keys2.read_bytes() == foreign
    after = harness.state()
    assert {key: value for key, value in after.items() if key != "calls"} == {
        key: value for key, value in before.items() if key != "calls"
    }
    assert after["calls"] == [
        "role list --output-format json",
        "user list --output-format json",
        "acl list --output-format json",
    ]
    assert "requires exclusive use" in result.stderr
    assert "was not modified" in result.stderr


def test_unrelated_acl_is_warned_and_never_removed(tmp_path: Path) -> None:
    """B8: deterministic root ACL repair leaves additional ACLs untouched."""
    harness = _new_harness(tmp_path)
    harness.enroll()
    state = harness.state()
    unrelated = {
        "path": "/vms/100",
        "ugid": "hubinetnext@pve",
        "roleid": "PVEAuditor",
        "propagate": 0,
    }
    state["acls"].append(unrelated)
    harness.write_state(state)
    harness.clear_calls()

    result = harness.run()

    assert result.returncode == 0
    assert unrelated in harness.state()["acls"]
    assert "unrelated ACLs; they were not changed" in result.stderr
    assert not any("acl delete" in call for call in harness.state()["calls"])


def test_token_creation_failure_emits_no_enrollment(tmp_path: Path) -> None:
    """B9: a failed final token operation cannot produce fake success output."""
    harness = _new_harness(tmp_path)
    state = harness.state()
    state["fail_token_add"] = True
    harness.write_state(state)

    result = harness.run()

    assert result.returncode != 0
    assert "API token creation failed" in result.stderr
    assert "HUBINET1-" not in result.stdout


def test_truncated_script_prefixes_never_invoke_provisioning(tmp_path: Path) -> None:
    """B10: curl truncation before the final main call executes no body."""
    harness = _new_harness(tmp_path)
    lines = _source().splitlines(keepends=True)
    prefixes = [lines[:1], lines[: len(lines) // 2], lines[:-1]]
    before = harness.state()

    for index, prefix in enumerate(prefixes):
        truncated = tmp_path / f"truncated-{index}.sh"
        truncated.write_text("".join(prefix))
        harness.run(script=truncated)

    assert harness.state() == before
    assert harness.authorized_keys2.exists() is False
    assert harness.helper.exists() is False
