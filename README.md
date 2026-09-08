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

### 1. Generate a dedicated SSH key

Generate a key used only for this helper, with no passphrase (the private key
must be usable non-interactively):

```bash
ssh-keygen -t ed25519 -N "" -f hubinet_ops -C "hubinet-ops package scan"
```

Copy the private half into the Home Assistant configuration directory as
`.ssh/hubinet_ops`, readable only by the Home Assistant runtime user:

```bash
install -o home-assistant -g home-assistant -m 0600 hubinet_ops \
  <config>/.ssh/hubinet_ops
```

### 2. Collect and verify the PVE host key

Collecting a host key over the network is not itself trust; only verify it
against a value read from the host out-of-band, such as the PVE console or
`pvesh`:

```bash
ssh-keyscan -t ed25519 <proxmox-host> > known_hosts.new
ssh-keygen -lf known_hosts.new
```

On the PVE host itself (console or an already-trusted session), print the
same key's fingerprint and compare it byte-for-byte against the value above
before trusting anything collected over the network:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Only once the fingerprints match, install the file as `.ssh/known_hosts` in
the Home Assistant configuration directory. The entry must match the
configured Proxmox **Host** value exactly, whether that value is a hostname or
IP address.

### 3. Restrict the key to the forced command

In root's `authorized_keys` on the PVE host, restrict the key from step 1 to
running only the helper, with no other capability:

```text
restrict,command="/usr/local/sbin/hubinet-package-scan-helper" ssh-ed25519 AAAA...
```

`restrict` disables port/agent/X11 forwarding, PTY allocation, and any other
use of the key beyond the forced command.

### 4. Allow root login for forced commands only

The connection authenticates as root, so root login must be permitted, but
only in a way compatible with the forced-command restriction above — never
general interactive root SSH access. In `sshd_config`:

```text
PermitRootLogin forced-commands-only
```

### 5. Reload after any trust-file change

Reload the integration after adding or changing `.ssh/hubinet_ops` or
`.ssh/known_hosts`. Package buttons and sensors are created only when both
files are present and non-empty at that reload; trust files added or changed
without a reload do not retroactively enable package entities, including for
LXCs discovered afterward. The integration connects as root on SSH port 22
using only that key, with host-key checking enabled and password and
SSH-agent authentication disabled.

See [PRODUCT.md](PRODUCT.md) for product scope,
[DEVELOPMENT.md](DEVELOPMENT.md) for the local development workflow, and
[UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation scope.
