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

Place the private half at `.ssh/hubinet_ops` inside the Home Assistant
configuration directory (`/config/.ssh/hubinet_ops` on Home Assistant OS and
Supervised installs), mode `0600`, and readable only by the Home Assistant
runtime. There is no single owning Unix user across every installation type
(container, OS, Supervised, Core), so set ownership/permissions the way that
installation type already expects config files to be readable by Home
Assistant — for example, on an install where Home Assistant runs as its own
user:

```bash
install -o <home-assistant-runtime-user> -g <home-assistant-runtime-group> \
  -m 0600 hubinet_ops <config>/.ssh/hubinet_ops
```

Treat the exact command as an environment-specific example, not something to
run verbatim.

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

### 4. Root SSH policy

The connection authenticates as root using only public-key auth, so the
host's existing SSH policy must already permit root public-key login (for
example `PermitRootLogin prohibit-password` or an equivalent public-key
policy most PVE hosts already use). Hubinet-Ops does not use password
authentication and does not require enabling it.

Do not change the host's global `sshd_config` `PermitRootLogin` policy
solely for Hubinet-Ops, and in particular do not set it to
`forced-commands-only` — that would force every root key on the host,
including unrelated administrator keys, into forced-command-only mode. The
actual security boundary is the per-key `restrict,command="..."` entry from
step 3 above, which already confines this one key to the helper regardless
of the host's general root login policy.

### 5. Reload after any trust-file change

Reload the integration after adding or changing `.ssh/hubinet_ops` or
`.ssh/known_hosts`. Package buttons and sensors are created only when both
files are present and non-empty at that reload; trust files added or changed
without a reload do not retroactively enable package entities, including for
LXCs discovered afterward. The integration connects as root on SSH port 22
using only that key, with host-key checking enabled and password and
SSH-agent authentication disabled.

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
rows — issues a new token and clears review, so confirming with an old
token is rejected (as `reviewed: false`, not an error). Review state lives
only in memory: it is gone after Home Assistant restarts, the integration
reloads, or the LXC's scan record is otherwise replaced. Stopping the LXC
does not clear a stored review; it just makes the sensor, and these
actions, unavailable until the LXC runs again.

See [PRODUCT.md](PRODUCT.md) for product scope,
[DEVELOPMENT.md](DEVELOPMENT.md) for the local development workflow, and
[UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation scope.
