"""Support for Proxmox VE."""

import logging
from pathlib import Path

import asyncssh

from homeassistant.const import CONF_HOST, CONF_TOKEN, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    AUTH_OTHER,
    AUTH_PAM,
    AUTH_PVE,
    CONF_AUTH_METHOD,
    CONF_NODE,
    CONF_NODES,
    CONF_PACKAGE_NODE,
    CONF_REALM,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PRIVATE_KEY,
    DEFAULT_REALM,
    PACKAGE_SCAN_KNOWN_HOSTS,
    PACKAGE_SCAN_PRIVATE_KEY,
)
from .coordinator import ProxmoxConfigEntry, ProxmoxCoordinator, node_device_info

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
]


_LOGGER = logging.getLogger(__name__)


def _read_legacy_transport_data(
    private_key_path: Path,
    known_hosts_path: Path,
    host: str,
    nodes: object,
) -> dict[str, str]:
    """Import an unambiguous version-3 file-based package identity."""
    if not private_key_path.exists() and not known_hosts_path.exists():
        return {}
    if (
        private_key_path.is_symlink()
        or known_hosts_path.is_symlink()
        or not private_key_path.is_file()
        or not known_hosts_path.is_file()
    ):
        raise ValueError("legacy package trust is incomplete")
    if (
        private_key_path.stat().st_size > 8192
        or known_hosts_path.stat().st_size > 256 * 1024
    ):
        raise ValueError("legacy package trust exceeds its migration bound")

    private_key_bytes = private_key_path.read_bytes()
    private_key = asyncssh.import_private_key(private_key_bytes)
    if private_key.get_algorithm() != "ssh-ed25519":
        raise ValueError("legacy package private key is not Ed25519")

    candidates: set[str] = set()
    for raw_line in known_hosts_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3 or parts[1] != "ssh-ed25519":
            continue
        host_patterns = parts[0].split(",")
        if host not in host_patterns and f"[{host}]:22" not in host_patterns:
            continue
        candidate = f"{parts[1]} {parts[2]}"
        imported = asyncssh.import_public_key(candidate.encode())
        if imported.get_algorithm() == "ssh-ed25519":
            candidates.add(candidate)
    if len(candidates) != 1:
        raise ValueError("legacy package host key is missing or ambiguous")

    node_names = {
        node[CONF_NODE]
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get(CONF_NODE), str)
    } if isinstance(nodes, list) else set()
    if len(node_names) != 1:
        raise ValueError("legacy package node is ambiguous")

    return {
        CONF_SSH_PRIVATE_KEY: private_key_bytes.decode("ascii"),
        CONF_SSH_HOST_KEY: candidates.pop(),
        CONF_PACKAGE_NODE: node_names.pop(),
    }


async def async_setup_entry(hass: HomeAssistant, entry: ProxmoxConfigEntry) -> bool:
    """Set up a ProxmoxVE from a config entry."""
    coordinator = ProxmoxCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    # Register node devices before forwarding platforms so that child devices
    # (VMs, containers, storages) can deterministically resolve their via_device.
    device_registry = dr.async_get(hass)
    for node_data in coordinator.data.values():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **node_device_info(coordinator, node_data),
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ProxmoxConfigEntry) -> bool:
    """Migrate old config entries."""

    # Migration for only the old binary sensors to new unique_id format
    if entry.version < 2:
        ent_reg = er.async_get(hass)
        for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            new_unique_id = (
                f"{entry.entry_id}_{entity_entry.unique_id.split('_')[-2]}_status"
            )

            _LOGGER.debug(
                "Migrating entity %s from old unique_id %s to new unique_id %s",
                entity_entry.entity_id,
                entity_entry.unique_id,
                new_unique_id,
            )
            ent_reg.async_update_entity(
                entity_entry.entity_id, new_unique_id=new_unique_id
            )

        hass.config_entries.async_update_entry(entry, version=2)

    # Migration for additional configuration options added to support API tokens
    if entry.version < 3:
        data = dict(entry.data)
        # If CONF_REALM wasn't there yet, extract from username
        if CONF_REALM not in data:
            data[CONF_REALM] = DEFAULT_REALM
            if "@" in data.get(CONF_USERNAME, ""):
                username, realm = data[CONF_USERNAME].split("@", 1)
                data[CONF_USERNAME] = username
                data[CONF_REALM] = realm

        realm = data[CONF_REALM]

        # If the realm is one of the base providers,
        # set the provider to match the realm.
        data[CONF_AUTH_METHOD] = realm if realm in (AUTH_PAM, AUTH_PVE) else AUTH_OTHER
        data.setdefault(CONF_TOKEN, False)

        hass.config_entries.async_update_entry(entry, data=data, version=3)

    # Version 4 removes the runtime dependency on /config/.ssh. Import only
    # complete, unambiguous legacy package trust and never alter the old files.
    if entry.version < 4:
        data = dict(entry.data)
        try:
            migrated_transport = await hass.async_add_executor_job(
                _read_legacy_transport_data,
                Path(hass.config.path(PACKAGE_SCAN_PRIVATE_KEY)),
                Path(hass.config.path(PACKAGE_SCAN_KNOWN_HOSTS)),
                data[CONF_HOST],
                data.get(CONF_NODES),
            )
        except (KeyError, OSError, TypeError, UnicodeError, ValueError, asyncssh.Error):
            _LOGGER.warning(
                "Could not safely import legacy Hubinet-Ops package SSH trust; "
                "native Proxmox entities will continue without package controls"
            )
        else:
            data.update(migrated_transport)
        hass.config_entries.async_update_entry(entry, data=data, version=4)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ProxmoxConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
