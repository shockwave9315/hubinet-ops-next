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

On each LXC's device page, **Review** shows the exact plan behind the pending
count as a notification, and **Approve** then confirms exactly that plan. A
later scan, even one with identical package rows, replaces the plan and clears
review, so an older Review cannot approve it. Review state lives only in
memory: it is gone after Home Assistant restarts, the integration reloads, or
the LXC's scan record is otherwise replaced. An observed stopped LXC loses
current package evidence and review; Scan is needed after it runs again.

The public actions are `hubinet_ops.easy_update` and
`hubinet_ops.scan_all_packages`. The former `get_package_plan` and
`confirm_package_review` actions were removed in 2026.9.1.15.

## Easy Update UX

| Profile | Scheduled Scan | Update | Autoremove after Update |
| --- | --- | --- | --- |
| EASY | On | One tap on the Easy Update card | Off (default) |
| YOLO | On | One tap on the Easy Update card | On, after the same successful Update, Health finished, and fresh positive candidates |
| MANUAL / ADVANCED | Optional | Scan -> Review -> Approve -> Update | Explicit Autoremove button |

There is no backend mode. All profiles use the existing backend. Manual
controls and Health remain available on the LXC device page.

### Easy Update card

The **Hubinet-Ops Easy Update** card ships with the integration. In normal
Lovelace resource storage mode, its native `module` resource is registered and
updated automatically. No resource URL, Mushroom, YAML, or entity mapping is
needed. Home Assistant loads the cards when the dashboard starts, using a
relative URL that follows the app's selected local or external HA address.

1. Install or update Hubinet-Ops through HACS and restart Home Assistant, then
   **refresh the browser** (or the companion app) once so the new card loads.
2. Turn on the automatic package scan once per host (see below).
3. Open a dashboard, choose **Edit -> Add card**, and search for **Hubinet**.
4. Choose **Hubinet-Ops Easy Update**, pick **one LXC**, optionally enable
   **YOLO** (remove unused packages after a successful update), and save.

The picker lists only LXCs that have the package Update button. The card
stores only the LXC device, so renaming entities does not break it.
A newly created LXC becomes selectable automatically after Hubinet-Ops
discovers it; add a card for it if you want it on the dashboard. Nothing has
to be recreated, and the integration never edits dashboards.

If you explicitly use **YAML resource mode**, declare the native module in
your existing `lovelace.resources` list instead. The integration does not edit
YAML files:

```yaml
lovelace:
  resources:
    - url: /hubinet_ops_static/hubinet-ops-cards.js?v=2026.9.1.20
      type: module
```

Update that version query when you upgrade the integration. One resource
loads all five Hubinet cards.

To uninstall, first remove all Hubinet-Ops host entries in **Settings -> Devices
& services**, then remove Hubinet-Ops in HACS. Removing the last host deletes
the integration's native resource; reloads and removal while another host
remains preserve it. Adding a host again restores the resource automatically.

HACS can delete the integration's files without invoking HA's entry-removal
hook. If the files were deleted first, remove the leftover entry through
**Settings -> Dashboards -> Resources**, matching only the relative path
`/hubinet_ops_static/hubinet-ops-cards.js` (with any version query). YAML
resource declarations must be removed from your own resource list.

| Card | Meaning | Tap |
| --- | --- | --- |
| Amber "7 aktualizacji" | Current scan found updates (security count, scan time) | Easy Update |
| Amber "Aktualizacja niedostępna dla tego LXC" | Updates found, but this LXC has no package Update button (no VM.Snapshot permission) | Open details |
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

### LXC card

The **Hubinet-Ops LXC** card loads together with the Easy Update card. Add it
the same way (**Edit -> Add card**, search **Hubinet**), pick one LXC, and
optionally enable YOLO. It shows for that one LXC:

- status and uptime, CPU and RAM with 24-hour sparklines, disk and network;
- the Easy Update row (same tap behavior as the Easy Update card);
- snapshots: choose one, then Create, Restore, or Delete;
- power: Start, Stop, and Restart.

**Stop, Restart, Restore, and Delete need a second tap** within 4 seconds; the
first tap only arms the button ("Na pewno? Dotknij ponownie"). The
confirmation covers only that exact operation: choosing another snapshot,
leaving the dashboard, or waiting longer cancels it, and the next tap arms
again. Start, Create,
Scan, and Update run on the first tap. Restore and Delete stay disabled until a
snapshot is selected. When the latest Proxmox refresh failed, the card shows
the orange no-data state and disables every action. Hold the card, or tap its
header, to open the LXC device page (administrators) or the status details.

**Tap a CPU, RAM, disk, or network tile** (or press Enter on it) to open Home
Assistant's own details dialog for that sensor with its history. This only
opens the dialog; it never starts anything.

### VM card

The **Hubinet-Ops VM** card does the same for one QEMU virtual machine: status
and uptime, CPU and RAM with 24-hour sparklines, disk and network (when those
sensors are enabled), snapshots (Create, Restore, Delete), and power: **Start**,
**Shut down** (ACPI, gracefully), **Stop** (hard power off), and **Restart**;
under **More**, **Reset** (hard restart) and **Hibernate**. Shut down, Stop,
Restart, Reset, Hibernate, Restore, and Delete need a second tap; Start and
Create run on the first tap. Package updates exist only for LXC, so the VM card
has no package section. Pause and Resume are not offered yet.

### Mini cards

**Hubinet-Ops LXC mini** and **Hubinet-Ops VM mini** show less: status, CPU and
RAM (tap for history), and one action. LXC mini offers the package action
(Update, Scan, or Details, as on the Easy Update card); VM mini offers Start when
the VM is stopped, or Shut down (second tap) when it runs. An LXC card saved
earlier with the compact layout now shows as LXC mini.

### Automatic package scan

Open **Settings > Devices & services > Hubinet-Ops > Configure** for each
Proxmox host, turn on **Scan automatically every day**, and choose the time
(default 06:00, Home Assistant local time). At that time Hubinet-Ops scans every
supported LXC on that host. Stopped or busy LXCs are skipped until the next day.
Scan never updates or removes packages; it is off until you turn it on.

`hubinet_ops.scan_all_packages` requests the same read-only Scan for every
supported LXC on every host at once; it takes no target or input.

See [PRODUCT.md](PRODUCT.md) for product scope,
[DEVELOPMENT.md](DEVELOPMENT.md) for the local development workflow, and
[UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation scope.
