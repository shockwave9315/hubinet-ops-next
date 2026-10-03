# Hubinet-Ops

Hubinet-Ops is a Home Assistant custom integration for Proxmox VE. It is a
domain-isolated fork of Home Assistant Core's `proxmoxve` integration from tag
`2026.9.1`, extended with LXC package Scan, Review, Update, Autoremove, Health,
and native snapshot controls.

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

If bootstrap refuses because `authorized_keys2` contains foreign or ambiguous
active keys, it leaves the file unchanged. Inspect it manually; only remove it
deliberately when none of those keys are relied upon, then rerun bootstrap.

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
operator view and confirm the exact plan behind that sensor's count:

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
the LXC's scan record is otherwise replaced. An observed stopped LXC loses
current package evidence and review; Scan is needed after it runs again.

## Easy Update UX

| Profile | Scheduled Scan | Update | Autoremove after Update |
| --- | --- | --- | --- |
| EASY | On | One tap on the Easy Update card | Off (default) |
| YOLO | On | One tap on the Easy Update card | On, after the same successful Update, Health finished, and fresh positive candidates |
| MANUAL / ADVANCED | Optional | Scan -> Review -> Approve -> Update | Explicit Autoremove button |

There is no backend mode. All profiles use the existing backend. Manual
controls and Health remain available on the LXC device page.

### Easy Update card

The **Hubinet-Ops Easy Update** card ships with the integration. No dashboard
resource URL, Mushroom, YAML, or entity mapping is needed.

1. Install or update Hubinet-Ops through HACS and restart Home Assistant, then
   **refresh the browser** (or the companion app) once so the new card loads.
2. Create the automatic Scan automation once (see below).
3. Open a dashboard, choose **Edit -> Add card**, and search for **Hubinet**.
4. Choose **Hubinet-Ops Easy Update**, pick **one LXC**, optionally enable
   **YOLO** (remove unused packages after a successful update), and save.

The card stores only the LXC device, so renaming entities does not break it.
A newly created LXC becomes selectable automatically after Hubinet-Ops
discovers it; add a card for it if you want it on the dashboard. Nothing has
to be recreated, and the integration never edits dashboards.

| Card | Meaning | Tap |
| --- | --- | --- |
| Amber "7 aktualizacji" | Current scan found updates (security count, scan time) | Easy Update |
| Green "System aktualny" | Current scan found none; shows last scan | Scan again |
| Orange "Wymagany restart" | Health reports reboot required | Scan again |
| Blue | Update, Autoremove, Scan, or Health is running | Nothing (no duplicates) |
| Red | Latest Update/Autoremove failed, or Health failed | Open details |
| Orange "Brak aktualnych danych z Proxmox" | The latest Proxmox refresh failed; the LXC may still run | Nothing |
| Grey | No current scan, failed scan, or LXC not running | Scan (if running) |

Hold the card to open the LXC device page with every manual control
(administrators; other users get the package sensor details). A tap is
your explicit authorization: it calls `hubinet_ops.easy_update`, which confirms
exactly the scan the card showed and starts the existing protected Update. If
another scan became current meanwhile, it is refused; check and tap again. A
successful Update leaves the count unknown (grey) until the next Scan. Closing
the browser never stops a started Update, and YOLO Autoremove runs in Home
Assistant, not in the browser. The Health result does not block YOLO; Health
only has to finish first.

Automations may call `hubinet_ops.easy_update` with `device_id`, optional
`autoremove`, and optional `expected_scan_attempt`; updates are still never
automatic unless you build such an automation yourself.

### Automatic package scan

Open **Settings > Devices & services > Hubinet-Ops > Configure** for each
Proxmox host, turn on **Scan automatically every day**, and choose the time
(default 06:00, Home Assistant local time). At that time Hubinet-Ops scans every
supported LXC on that host. Stopped or busy LXCs are skipped until the next day.
Scan never updates or removes packages; it is off until you turn it on.

### Automatic Scan blueprint

1. Install or update Hubinet-Ops through HACS, then restart Home Assistant if
   HACS requests it. Hubinet-Ops provisions the blueprint when it loads.
2. Open **Settings > Automations & scenes > Blueprints** and create **one
   automation for the whole installation** from
   **Hubinet-Ops — automatyczny skan aktualizacji**. Configure only the daily
   time, whether to scan after HA starts, and the startup delay. Defaults are
   04:00 in HA's timezone, startup Scan enabled, and 60 seconds.

**Automatic Scan requires no LXC selection.** It always considers all currently
supported Hubinet-Ops package LXCs across loaded integration entries. Stopped
or busy targets do not prevent other targets from starting. The existing
backend owns scan validation, background tasks, and concurrency; Scan never
updates or removes packages. You can also request the same read-only operation
with `hubinet_ops.scan_all_packages`, which takes no target or input.

The shipped source lives under `custom_components/hubinet_ops/blueprints/` so
HACS installs it with the integration. Hubinet-Ops synchronizes its managed copy
under `/config/blueprints/automation/hubinet_ops/` on setup/reload. Do not edit
that copy: changes are replaced by the shipped version. To customize it, copy it
under your own namespace/name. Provisioning never creates or enables user
automations.

See [PRODUCT.md](PRODUCT.md) for product scope,
[DEVELOPMENT.md](DEVELOPMENT.md) for the local development workflow, and
[UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation scope.
