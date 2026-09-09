# Hubinet-Ops

Hubinet-Ops is a Home Assistant custom integration for Proxmox VE. It is a
domain-isolated fork of Home Assistant Core's `proxmoxve` integration from tag
`2026.9.1`, extended with manual LXC pending-package scans and review.

## Install and enroll

1. Add `https://github.com/shockwave9315/hubinet-ops-next` to HACS as a custom
   integration repository, then install **Hubinet-Ops**.
2. Restart Home Assistant and add **Hubinet-Ops** from **Settings > Devices &
   services**.
3. Choose **Guided setup (recommended)** and enter the Proxmox host, API port,
   and SSL-verification preference.
4. Run the single release-pinned command Home Assistant displays, once as root
   on that Proxmox VE host.
5. Paste the single `HUBINET1-...` enrollment value it prints.

Home Assistant validates the dedicated API token, pinned SSH host key, SSH
authentication, installed helper protocol, and local PVE node before it creates
the config entry. When the entry appears, setup is complete: native Proxmox
entities and local-node LXC package entities are available without another
reload.

The **Existing credentials (advanced)** path preserves the upstream-compatible
manual Proxmox authentication flow. It does not provision package-control SSH
trust.

## Repair and re-enroll

- Run the same displayed bootstrap command without options to repair the fixed
  role, user, ACL, and release helper. Existing API and SSH credentials remain
  unchanged, so Home Assistant normally needs no interaction.
- For credential recovery, run the command with `--reset`, then choose
  **Reconfigure > Re-enroll** in Home Assistant and paste the new
  `HUBINET1-...` value. This replaces the old Hubinet API token and SSH client
  key.

Treat every enrollment value as sensitive credential material. Hubinet-Ops
does not persist the raw blob, but it remains valid while its contained
credentials remain valid; discard terminal and clipboard copies after setup.

Hubinet-Ops exclusively owns `/root/.ssh/authorized_keys2` for this release.
Bootstrap refuses foreign contents rather than merging or rewriting them. It
never modifies `/root/.ssh/authorized_keys`,
`/etc/pve/priv/authorized_keys`, or `sshd_config`.

## LXC package scan

The package scan refreshes APT metadata, simulates an upgrade, and reads dpkg
and reboot evidence. It does not install, remove, repair, or upgrade packages.

Current package scanning is single-host. Package controls appear only for LXCs
whose upstream-discovered node matches the local node authenticated during
guided setup. The integration connects to that configured host as root on SSH
port 22 using the enrolled in-memory private key and pinned ED25519 host key;
password and SSH-agent authentication remain disabled.

## Package review

Two actions, available on each LXC's pending-package-updates sensor, let an
operator view and confirm the exact plan behind that sensor's count before a
future update feature can use it:

- **`hubinet_ops.get_package_plan`** returns the latest scan status and,
  only for a successful scan, its exact package rows and a token. It never
  starts a scan and never changes review state.
- **`hubinet_ops.confirm_package_review`** takes that `token` and confirms
  the plan it belongs to as reviewed, if it is still the current one.

The token is not a password or a secret; it only ties a confirmation to one
specific scan observation. A later scan — even one with identical package
rows — issues a new token and clears review, so confirming with an old token is
rejected (as `reviewed: false`, not an error). Review state lives only in
memory: it is gone after Home Assistant restarts, the integration reloads, or
the LXC's scan record is otherwise replaced. Stopping the LXC does not clear a
stored review; it just makes the sensor, and these actions, unavailable until
the LXC runs again.

See [PRODUCT.md](PRODUCT.md) for product scope,
[DEVELOPMENT.md](DEVELOPMENT.md) for the local development workflow, and
[UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation scope.
