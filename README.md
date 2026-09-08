# Hubinet-Ops

Hubinet-Ops is a Home Assistant custom integration for Proxmox VE. It is a
domain-isolated fork of Home Assistant Core's `proxmoxve` integration from tag
`2026.9.1`, extended with manual LXC pending-package scans.

Copy `custom_components/hubinet_ops` into the `custom_components` directory of
a Home Assistant `2026.9.1` installation, restart Home Assistant, and add
**Hubinet-Ops** from **Settings > Devices & services**.

## LXC package scan

The package scan refreshes APT metadata, simulates an upgrade, and reads dpkg
and reboot evidence. It does not install, remove, repair, or upgrade packages.

Current package scanning is single-host. On the PVE host configured as the
integration's **Host**, install the helper as a root-owned executable:

```bash
install -o root -g root -m 0755 deploy/hubinet-package-scan-helper.py \
  /usr/local/sbin/hubinet-package-scan-helper
```

Restrict a dedicated SSH public key to the helper in root's
`authorized_keys`:

```text
restrict,command="/usr/local/sbin/hubinet-package-scan-helper" ssh-ed25519 AAAA...
```

In the Home Assistant configuration directory, store the corresponding
unencrypted private key as `.ssh/hubinet_ops` and verified known-host material
as `.ssh/known_hosts`. The known-host entry must match the configured Proxmox
**Host** value, whether that value is a hostname or IP address. Keep the private
key readable only by the Home Assistant runtime user.

Reload the integration after adding or changing either trust file. Package
buttons and sensors are created only when both files are present and non-empty.
The integration connects as root on SSH port 22 using only that key, with host-
key checking enabled and password and SSH-agent authentication disabled.

See [PRODUCT.md](PRODUCT.md) for product scope,
[DEVELOPMENT.md](DEVELOPMENT.md) for the local development workflow, and
[UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation scope.
