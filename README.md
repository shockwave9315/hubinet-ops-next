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

The optional blueprints require Home Assistant `2026.9.1` or newer and the
Hubinet-Ops package entities. All inputs in an Update script must belong to
**the same LXC**. The selectors list Hubinet-Ops entities; choose the specified
Scan/Update/Autoremove buttons and package sensors by their labels.

| Profile | Scheduled Scan | Update | Autoremove after Update |
| --- | --- | --- | --- |
| EASY | On | One explicit script click | Off (default) |
| YOLO | On | One explicit script click | On, with successful Update and fresh positive candidates |
| MANUAL / ADVANCED | Optional | Scan -> Review -> Approve -> Update | Explicit Autoremove button |

There is no backend mode. All profiles use the existing backend. Manual
controls and Health remain available. Inspecting individual package names is
optional; hold the example card to open Review's full exact plan.

### Import and configure the blueprints

In **Settings > Automations & scenes > Blueprints**, import these GitHub file
URLs (available on `main` after merge):

- [Daily package Scan](https://github.com/shockwave9315/hubinet-ops-next/blob/main/blueprints/automation/hubinet_ops_daily_package_scan.yaml)
- [One-click LXC Update](https://github.com/shockwave9315/hubinet-ops-next/blob/main/blueprints/script/hubinet_ops_one_click_update.yaml)

For testing the draft PR, replace `main` in those URLs with
`feat/2026.9.1.12-easy-update-ux`. Alternatively, copy the files into
`/config/blueprints/automation/hubinet_ops/` and
`/config/blueprints/script/hubinet_ops/` respectively, then reload blueprints.

Create an automation from **Daily package Scan**, select each participating
LXC's **Scan pending packages** button, and choose the daily time. Defaults are
04:00 in Home Assistant's configured timezone, Scan after HA start enabled,
and a 60-second startup delay. Each button is pressed separately; unavailable
or rejected targets do not stop the remaining requests. Scans run in the
existing backend, which owns concurrency. Nothing discovers targets or
updates packages automatically.

Create one script from **One-click LXC Update** for each chosen LXC. Map its
pending-package sensor, package-update sensor, Update button, unused-packages
sensor, and Autoremove button. Give it a recognizable name, for example
`script.nextcloud_easy_update`. Leave **Autoremove after successful Update** off
for EASY, or turn it on for YOLO. That option is your explicit policy choice;
no second confirmation is added.

A click uses the current successful non-empty Scan, calls
`hubinet_ops.get_package_plan`, takes the response at the configured sensor's
entity ID, confirms that exact token with `hubinet_ops.confirm_package_review`,
then presses Update once. It does not press Review or Scan. Once Update accepts,
the backend owns fresh exact-plan validation, safety snapshot, mutation,
dpkg/liveness checks, exact snapshot cleanup, cleanup observation, and Health.
`PLAN_CHANGED` stays a backend failure: obtain a new Scan and click again.

With Autoremove off, the script finishes after starting Update. With it on,
the script observes this new Update attempt, proceeds only after SUCCESS and
fresh positive unused-package evidence, and presses Autoremove once when its
existing button becomes available. Automatic post-Update Health can briefly
keep that button unavailable. A button whose state is `unknown` may simply
have never been pressed; availability is distinct from that state.

The observation budget is **one hour**, shared by Update completion and button
availability. This exceeds a single operation's current plan (300s), snapshot
create/delete polling (about 240s each), mutation (1860s), liveness, cleanup
observation (300s), and Health (90s) bounds. A long wait behind another LXC's
update can still exhaust it. Timeout or stopping the script stops only YAML
orchestration; the backend operation continues, and no Autoremove is started.
Scripts are `single` mode: another tap while one is running is ignored.

### Mushroom card

Install [Mushroom](https://github.com/piitaya/lovelace-mushroom), then copy
[mushroom_easy_update.yaml](examples/dashboard/mushroom_easy_update.yaml) into
a dashboard's manual card editor. It uses Mushroom's
[current Template card](https://github.com/piitaya/lovelace-mushroom/blob/main/docs/cards/template.md)
and `color` option. Replace the five unique entity IDs (some appear more than
once) and the display name with your LXC's entities and configured script.

The Polish example shows green for zero current updates, amber for a positive
count and security count, blue during Update, red for a failed latest Update
or failed Health, and grey without current Scan evidence. Tap starts your
script; hold presses Review. A successful Update invalidates pending evidence,
so grey until the next Scan is expected. No exact package list is loaded by
the card, and the card contains no mutation or authorization logic.

See [PRODUCT.md](PRODUCT.md) for product scope,
[DEVELOPMENT.md](DEVELOPMENT.md) for the local development workflow, and
[UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation scope.
