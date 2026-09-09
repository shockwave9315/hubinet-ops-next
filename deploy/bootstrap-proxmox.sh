#!/bin/sh
# Hubinet-Ops Next guided enrollment bootstrap. This script owns only the
# hardcoded resources below and deliberately provides no generic provisioning.
set -eu

main() {
  RELEASE_VERSION="2026.9.1.3"
  HELPER_SHA256="af398ae45bf319e09eed97a5c380293d9fbe8000b7d4eff5c11a104b6886a8f4"
  REPOSITORY="shockwave9315/hubinet-ops-next"
  HELPER_URL="https://raw.githubusercontent.com/${REPOSITORY}/${RELEASE_VERSION}/deploy/hubinet-package-scan-helper.py"
  BOOTSTRAP_URL="https://raw.githubusercontent.com/${REPOSITORY}/${RELEASE_VERSION}/deploy/bootstrap-proxmox.sh"

  PVE_USER="hubinetnext@pve"
  PVE_ROLE="HubinetOpsNext"
  PVE_TOKEN_ID="ha"
  PVE_PRIVILEGES="Datastore.Audit Sys.Audit Sys.PowerMgmt VM.Audit VM.PowerMgmt VM.Snapshot"
  FORCED_COMMAND="/usr/local/sbin/hubinet-package-scan-helper"
  HELPER_TARGET="/usr/local/sbin/hubinet-package-scan-helper"
  PVE_DIRECTORY="/etc/pve"
  PVE_LOCAL="/etc/pve/local"
  HOST_KEY_FILE="/etc/ssh/ssh_host_ed25519_key.pub"
  ROOT_SSH_DIRECTORY="/root/.ssh"
  AUTHORIZED_KEYS2="/root/.ssh/authorized_keys2"

  fail() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
  }

  notice() {
    printf 'Hubinet-Ops: %s\n' "$1" >&2
  }

  # Tests may redirect only product-owned filesystem paths into a disposable
  # root. Production has no path option and always uses the constants above.
  if [ -n "${_HUBINET_BOOTSTRAP_TEST_ROOT-}" ]; then
    [ "${_HUBINET_BOOTSTRAP_TESTING-}" = "1" ] || fail "test root requires the test-only guard"
    case "$_HUBINET_BOOTSTRAP_TEST_ROOT" in
      /*) ;;
      *) fail "test root must be absolute" ;;
    esac
    PVE_DIRECTORY="${_HUBINET_BOOTSTRAP_TEST_ROOT}/etc/pve"
    PVE_LOCAL="${PVE_DIRECTORY}/local"
    HOST_KEY_FILE="${_HUBINET_BOOTSTRAP_TEST_ROOT}/etc/ssh/ssh_host_ed25519_key.pub"
    ROOT_SSH_DIRECTORY="${_HUBINET_BOOTSTRAP_TEST_ROOT}/root/.ssh"
    AUTHORIZED_KEYS2="${ROOT_SSH_DIRECTORY}/authorized_keys2"
    HELPER_TARGET="${_HUBINET_BOOTSTRAP_TEST_ROOT}${FORCED_COMMAND}"
  fi
  HELPER_DIRECTORY=${HELPER_TARGET%/*}

  RESET=0
  case "${1-}" in
    "") ;;
    --reset) RESET=1 ;;
    --help)
      printf '%s\n' "Usage: bootstrap-proxmox.sh [--reset]"
      printf '%s\n' "  --reset  Rotate Hubinet-owned credentials for re-enrollment."
      exit 0
      ;;
    *)
      printf '%s\n' "ERROR: unknown option: ${1}" >&2
      exit 2
      ;;
  esac
  if [ "$#" -gt 1 ]; then
    printf '%s\n' "ERROR: expected at most one option" >&2
    exit 2
  fi

  [ "$(id -u)" -eq 0 ] || fail "run this command as root on the Proxmox VE host"

  for tool in pveversion pvesh pveum pct python3 curl sha256sum install mktemp ssh-keygen sshd readlink awk mv rm stat; do
    command -v "$tool" >/dev/null 2>&1 || fail "required tool is missing: ${tool}"
  done
  pveversion >/dev/null 2>&1 || fail "this does not appear to be a Proxmox VE host"
  if [ ! -d "$PVE_DIRECTORY" ] || [ ! -e "$PVE_LOCAL" ]; then
    fail "/etc/pve is not usable"
  fi
  pvesh get /version --output-format json >/dev/null 2>&1 || fail "the local PVE API is not usable"

  SSHD_EFFECTIVE=$(sshd -T 2>/dev/null) || fail "could not read effective sshd configuration"
  printf '%s\n' "$SSHD_EFFECTIVE" | awk '
    $1 == "authorizedkeysfile" {
      for (i = 2; i <= NF; i++) if ($i == ".ssh/authorized_keys2") found = 1
    }
    END { exit(found ? 0 : 1) }
  ' || fail "effective sshd AuthorizedKeysFile does not include .ssh/authorized_keys2"

  # Inspect all fixed resources before mutation. Foreign authorized_keys2
  # contents are an unconditional preflight failure for this release.
  ROLE_JSON=$(pveum role list --output-format json) || fail "could not inspect PVE roles"
  USER_JSON=$(pveum user list --output-format json) || fail "could not inspect PVE users"
  ACL_JSON=$(pveum acl list --output-format json) || fail "could not inspect PVE ACLs"
  USER_EXISTS=$(printf '%s' "$USER_JSON" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
print("1" if any(row.get("userid") == "hubinetnext@pve" for row in rows) else "0")
') || fail "could not parse PVE user state"
  TOKEN_JSON="[]"
  if [ "$USER_EXISTS" -eq 1 ]; then
    TOKEN_JSON=$(pveum user token list "$PVE_USER" --output-format json) || fail "could not inspect the Hubinet token"
  fi
  TOKEN_EXISTS=$(printf '%s' "$TOKEN_JSON" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
print("1" if any(row.get("tokenid") == "ha" for row in rows) else "0")
') || fail "could not parse PVE token state"

  SSH_CREDENTIAL_STATE="broken"
  if [ -e "$AUTHORIZED_KEYS2" ] || [ -L "$AUTHORIZED_KEYS2" ]; then
    if [ ! -f "$AUTHORIZED_KEYS2" ] || [ -L "$AUTHORIZED_KEYS2" ]; then
      fail "guided enrollment stopped: authorized_keys2 is not a regular file and was not modified"
    fi
    SSH_CREDENTIAL_STATE=$(python3 - "$AUTHORIZED_KEYS2" <<'PY'
import re
import sys

pattern = re.compile(
    r'^restrict,command="/usr/local/sbin/hubinet-package-scan-helper" '
    r'ssh-ed25519 [A-Za-z0-9+/]+={0,3} hubinet-ops$'
)
try:
    with open(sys.argv[1], encoding="ascii") as handle:
        lines = [
            line.strip()
            for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        ]
except (OSError, UnicodeError):
    print("foreign")
else:
    if len(lines) == 1 and pattern.fullmatch(lines[0]):
        print("valid")
    elif any(not line.endswith(" hubinet-ops") for line in lines):
        print("foreign")
    else:
        print("broken")
PY
    ) || fail "could not inspect authorized_keys2"
  fi
  if [ "$SSH_CREDENTIAL_STATE" = "foreign" ]; then
    fail "guided enrollment stopped and authorized_keys2 was not modified: Hubinet-Ops requires exclusive use of /root/.ssh/authorized_keys2 for this release"
  fi
  if [ -e "$HELPER_TARGET" ] || [ -L "$HELPER_TARGET" ]; then
    if [ ! -f "$HELPER_TARGET" ] || [ -L "$HELPER_TARGET" ]; then
      fail "the helper target is not a regular file"
    fi
  fi
  [ -d "$HELPER_DIRECTORY" ] || fail "the helper installation directory is missing"

  HELPER_EXACT=0
  if [ -f "$HELPER_TARGET" ] && [ ! -L "$HELPER_TARGET" ]; then
    CURRENT_HELPER_SHA256=$(sha256sum "$HELPER_TARGET" | awk '{print $1}') || fail "could not inspect the installed helper"
    CURRENT_HELPER_METADATA=$(stat -c '%u:%g:%a' "$HELPER_TARGET") || fail "could not inspect installed helper permissions"
    if [ "$CURRENT_HELPER_SHA256" = "$HELPER_SHA256" ] && [ "$CURRENT_HELPER_METADATA" = "0:0:755" ]; then
      HELPER_EXACT=1
    fi
  fi

  WORK_DIR=$(mktemp -d /tmp/hubinet-bootstrap.XXXXXX)
  HELPER_INSTALL=""
  AUTHORIZED_INSTALL=""
  cleanup() {
    if [ -n "$HELPER_INSTALL" ]; then
      rm -f -- "$HELPER_INSTALL"
    fi
    if [ -n "$AUTHORIZED_INSTALL" ]; then
      rm -f -- "$AUTHORIZED_INSTALL"
    fi
    rm -rf -- "$WORK_DIR"
  }
  trap cleanup EXIT HUP INT TERM
  umask 077

  ROLE_EXISTS=$(printf '%s' "$ROLE_JSON" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
print("1" if any(row.get("roleid") == "HubinetOpsNext" for row in rows) else "0")
') || fail "could not parse PVE role state"
  ROLE_EXACT=$(printf '%s' "$ROLE_JSON" | python3 -c '
import json, re, sys
wanted = set("Datastore.Audit Sys.Audit Sys.PowerMgmt VM.Audit VM.PowerMgmt VM.Snapshot".split())
rows = json.load(sys.stdin)
row = next((row for row in rows if row.get("roleid") == "HubinetOpsNext"), None)
actual = set(filter(None, re.split(r"[\s,]+", (row or {}).get("privs", ""))))
print("1" if row is not None and actual == wanted else "0")
') || fail "could not parse PVE role privileges"
  if [ "$ROLE_EXISTS" -eq 0 ]; then
    notice "creating role ${PVE_ROLE}"
    pveum role add "$PVE_ROLE" --privs "$PVE_PRIVILEGES"
  elif [ "$ROLE_EXACT" -eq 0 ]; then
    notice "updating role ${PVE_ROLE} to the exact Hubinet privilege set"
    pveum role modify "$PVE_ROLE" --privs "$PVE_PRIVILEGES"
  fi

  if [ "$USER_EXISTS" -eq 0 ]; then
    notice "creating dedicated user ${PVE_USER}"
    pveum user add "$PVE_USER" --enable 1 --expire 0
  else
    USER_EXACT=$(printf '%s' "$USER_JSON" | python3 -c '
import json, sys
row = next(row for row in json.load(sys.stdin) if row.get("userid") == "hubinetnext@pve")
print("1" if row.get("enable", 1) == 1 and row.get("expire", 0) in (0, None) else "0")
') || fail "could not parse Hubinet user attributes"
    if [ "$USER_EXACT" -eq 0 ]; then
      notice "enabling the dedicated user and clearing its expiry"
      pveum user modify "$PVE_USER" --enable 1 --expire 0
    fi
  fi

  ACL_EXACT=$(printf '%s' "$ACL_JSON" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
print("1" if any(
    row.get("path") == "/"
    and row.get("ugid") == "hubinetnext@pve"
    and row.get("roleid") == "HubinetOpsNext"
    and row.get("propagate", 1) == 1
    for row in rows
) else "0")
') || fail "could not parse PVE ACL state"
  if [ "$ACL_EXACT" -eq 0 ]; then
    notice "granting ${PVE_ROLE} to ${PVE_USER} at /"
    pveum acl modify / --users "$PVE_USER" --roles "$PVE_ROLE" --propagate 1
  fi
  printf '%s' "$ACL_JSON" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
if any(
    row.get("ugid") == "hubinetnext@pve"
    and (row.get("path") != "/" or row.get("roleid") != "HubinetOpsNext")
    for row in rows
):
    print("Hubinet-Ops: WARNING: the dedicated user has unrelated ACLs; they were not changed", file=sys.stderr)
' || fail "could not inspect unrelated ACLs"

  if [ "$HELPER_EXACT" -eq 0 ]; then
    HELPER_DOWNLOAD="$WORK_DIR/helper.py"
    notice "downloading the release-pinned helper"
    curl -fsSL "$HELPER_URL" -o "$HELPER_DOWNLOAD" || fail "could not download the helper"
    printf '%s  %s\n' "$HELPER_SHA256" "$HELPER_DOWNLOAD" | sha256sum -c - >/dev/null 2>&1 || fail "helper SHA-256 verification failed"
    python3 -m py_compile "$HELPER_DOWNLOAD" || fail "helper Python self-check failed"
    HELPER_INSTALL=$(mktemp "${HELPER_DIRECTORY}/.hubinet-package-scan-helper.XXXXXX") || fail "could not stage the helper"
    install -o root -g root -m 0755 "$HELPER_DOWNLOAD" "$HELPER_INSTALL"
    notice "installing ${FORCED_COMMAND}"
    mv -f -- "$HELPER_INSTALL" "$HELPER_TARGET"
    HELPER_INSTALL=""
  else
    notice "the release-pinned helper is already installed"
  fi

  PROBE_RESPONSE=$(printf '%s' '{"protocol_version":1,"operation":"probe"}' | "$HELPER_TARGET") || fail "installed helper probe failed"
  LOCAL_NODE=$(printf '%s' "$PROBE_RESPONSE" | python3 -c '
import json, re, sys
value = json.load(sys.stdin)
node = value.get("node")
if (
    value.get("protocol_version") != 1
    or value.get("operation") != "probe"
    or value.get("ok") is not True
    or not isinstance(node, str)
    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", node) is None
):
    raise SystemExit(1)
print(node)
') || fail "installed helper returned an invalid probe"

  if [ "$TOKEN_EXISTS" -eq 1 ] && [ "$RESET" -eq 0 ]; then
    if [ "$SSH_CREDENTIAL_STATE" = "valid" ]; then
      notice "deterministic resources were reconciled; enrollment credentials were unchanged"
      exit 0
    fi
    printf '%s\n' "ERROR: deterministic resources were reconciled, but Hubinet SSH credentials are missing or broken." >&2
    printf '%s\n' "Credential re-enrollment is required. Run:" >&2
    printf 'curl -fsSL %s | bash -s -- --reset\n' "$BOOTSTRAP_URL" >&2
    printf '%s\n' "Then in Home Assistant choose Reconfigure → Re-enroll and paste the new value." >&2
    exit 1
  fi

  [ -f "$HOST_KEY_FILE" ] || fail "PVE ED25519 SSH host public key is missing"
  HOST_KEY=$(awk 'NR == 1 { print $1 " " $2 } NR > 1 { bad = 1 } END { if (NR != 1 || bad) exit 1 }' "$HOST_KEY_FILE") || fail "PVE SSH host public key is malformed"
  printf '%s\n' "$HOST_KEY" | ssh-keygen -l -f - >/dev/null 2>&1 || fail "PVE SSH host public key is invalid"

  CLIENT_KEY="$WORK_DIR/hubinet_ops"
  ssh-keygen -q -t ed25519 -N '' -C hubinet-ops -f "$CLIENT_KEY" || fail "could not generate the enrollment SSH key"
  PUBLIC_KEY=$(awk 'NR == 1 { print $1 " " $2 " hubinet-ops" } NR > 1 { bad = 1 } END { if (NR != 1 || bad) exit 1 }' "${CLIENT_KEY}.pub") || fail "generated SSH public key is malformed"

  TOKEN_SECRET_FILE="$WORK_DIR/token_secret"
  build_enrollment() {
    python3 - "$TOKEN_SECRET_FILE" "$CLIENT_KEY" "$HOST_KEY_FILE" <<'PY'
import base64
import json
import sys

with open(sys.argv[1], encoding="ascii") as handle:
    token = handle.read().strip()
with open(sys.argv[2], "rb") as handle:
    private_key = base64.b64encode(handle.read()).decode("ascii")
with open(sys.argv[3], encoding="ascii") as handle:
    parts = handle.read().split()
if len(parts) < 2 or parts[0] != "ssh-ed25519":
    raise SystemExit(1)
payload = {"v": 1, "t": token, "k": private_key, "h": f"{parts[0]} {parts[1]}"}
encoded = base64.urlsafe_b64encode(
    json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
).rstrip(b"=").decode("ascii")
print(f"HUBINET1-{encoded}")
PY
  }

  # Prove enrollment construction before changing either credential.
  printf 'preflight-only\n' > "$TOKEN_SECRET_FILE"
  build_enrollment >/dev/null || fail "could not prepare enrollment output"

  if [ ! -e "$ROOT_SSH_DIRECTORY" ]; then
    install -d -o root -g root -m 0700 "$ROOT_SSH_DIRECTORY"
  elif [ ! -d "$ROOT_SSH_DIRECTORY" ] || [ -L "$ROOT_SSH_DIRECTORY" ]; then
    fail "/root/.ssh is not a regular directory"
  fi
  AUTHORIZED_TEMP="$WORK_DIR/authorized_keys2"
  printf 'restrict,command="%s" %s\n' "$FORCED_COMMAND" "$PUBLIC_KEY" > "$AUTHORIZED_TEMP"
  AUTHORIZED_INSTALL=$(mktemp "${ROOT_SSH_DIRECTORY}/.hubinet-authorized_keys2.XXXXXX") || fail "could not stage authorized_keys2"
  install -o root -g root -m 0600 "$AUTHORIZED_TEMP" "$AUTHORIZED_INSTALL"
  mv -f -- "$AUTHORIZED_INSTALL" "$AUTHORIZED_KEYS2"
  AUTHORIZED_INSTALL=""
  notice "installed the restricted Hubinet key in /root/.ssh/authorized_keys2"

  # Token removal/creation is deliberately last. PVE reveals a new secret once.
  if [ "$TOKEN_EXISTS" -eq 1 ]; then
    notice "--reset requested: rotating token ${PVE_USER}!${PVE_TOKEN_ID}"
    pveum user token remove "$PVE_USER" "$PVE_TOKEN_ID"
  fi
  TOKEN_RESULT=$(pveum user token add "$PVE_USER" "$PVE_TOKEN_ID" --privsep 0 --output-format json) || fail "API token creation failed"
  printf '%s' "$TOKEN_RESULT" | python3 -c '
import json, sys
value = json.load(sys.stdin).get("value")
if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(ch.isspace() for ch in value):
    raise SystemExit(1)
sys.stdout.write(value)
' > "$TOKEN_SECRET_FILE" || fail "PVE did not return a usable API token secret"

  ENROLLMENT=$(build_enrollment) || fail "could not construct enrollment output"
  printf '\nHubinet enrollment value (treat as sensitive and discard after setup):\n%s\n' "$ENROLLMENT"
  printf 'Local package node: %s\n' "$LOCAL_NODE" >&2
}

main "$@"
