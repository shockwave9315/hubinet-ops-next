# Hubinet-Ops

Hubinet-Ops is a Home Assistant custom integration for Proxmox VE. This first
baseline is a domain-isolated fork of Home Assistant Core's `proxmoxve`
integration from tag `2026.9.1`.

Copy `custom_components/hubinet_ops` into the `custom_components` directory of
a Home Assistant `2026.9.1` installation, restart Home Assistant, and add
**Hubinet-Ops** from **Settings > Devices & services**.

See [UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation
scope.

## LXC package scan

Each LXC device has a **Scan pending packages** button and a **Pending package
updates** sensor. A scan refreshes APT metadata, simulates `apt-get upgrade`,
and reads dpkg/reboot evidence. It never installs, removes, or upgrades a
package.

Guest execution is deliberately outside the Proxmox API connection. Install
`deploy/hubinet-package-scan-helper.py` as a root-owned executable on every PVE
node that hosts scannable containers, then restrict a dedicated SSH public key
to that command, for example:

```text
restrict,command="/usr/local/sbin/hubinet-package-scan-helper" ssh-ed25519 AAAA...
```

Place the corresponding private key in the Home Assistant configuration
directory as `.ssh/hubinet_ops`, and a pinned host-key file as
`.ssh/known_hosts`. The host-key entries must cover the PVE node names returned
by the existing Proxmox coordinator. The integration uses OpenSSH in batch
mode as `root`, rejects remote command text, and accepts only a bounded JSON
request containing the coordinator-provided LXC VMID.
