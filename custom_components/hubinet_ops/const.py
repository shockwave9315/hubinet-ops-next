"""Constants for ProxmoxVE."""

from enum import StrEnum

DOMAIN = "hubinet_ops"
CONF_AUTH_METHOD = "auth_method"
CONF_REALM = "realm"
CONF_NODE = "node"
CONF_NODES = "nodes"
CONF_TOKEN_ID = "token_id"
CONF_TOKEN_SECRET = "token_value"
CONF_VMS = "vms"
CONF_CONTAINERS = "containers"
CONF_SSH_PRIVATE_KEY = "ssh_private_key"
CONF_SSH_HOST_KEY = "ssh_host_key"
CONF_PACKAGE_NODE = "package_node"

CONF_USER = "user"

NODE_ONLINE = "online"
VM_CONTAINER_RUNNING = "running"

STORAGE_ACTIVE = 1
STORAGE_SHARED = 1
STORAGE_ENABLED = 1
STATUS_OK = "OK"

AUTH_PAM = "pam"
AUTH_PVE = "pve"
AUTH_OTHER = "other"
AUTH_METHODS = [AUTH_PAM, AUTH_PVE, AUTH_OTHER]

DEFAULT_PORT = 8006
DEFAULT_REALM = AUTH_PAM
DEFAULT_TIMEOUT = 30
DEFAULT_VERIFY_SSL = True
TYPE_VM = 0
TYPE_CONTAINER = 1
UPDATE_INTERVAL = 60

# Legacy version-3 trust paths, read only by the one-time entry migration.
PACKAGE_SCAN_PRIVATE_KEY = ".ssh/hubinet_ops"
PACKAGE_SCAN_KNOWN_HOSTS = ".ssh/known_hosts"

GUIDED_USERNAME = "hubinetnext@pve"
GUIDED_TOKEN_ID = "ha"
INTEGRATION_VERSION = "2026.9.1.4"
EXPECTED_HELPER_VERSION = 4
BOOTSTRAP_COMMAND = (
    "curl -fsSL "
    "https://raw.githubusercontent.com/shockwave9315/hubinet-ops-next/"
    f"{INTEGRATION_VERSION}/deploy/bootstrap-proxmox.sh | bash"
)
RESET_BOOTSTRAP_COMMAND = f"{BOOTSTRAP_COMMAND} -s -- --reset"


class ProxmoxPermission(StrEnum):
    """Proxmox permissions."""

    POWER = "VM.PowerMgmt"
    SNAPSHOT = "VM.Snapshot"
    SYSAUDIT = "Sys.Audit"
    SYSPOWER = "Sys.PowerMgmt"
    VMAUDIT = "VM.Audit"
