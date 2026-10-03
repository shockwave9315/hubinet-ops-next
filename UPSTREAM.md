# Upstream provenance

Hubinet-Ops is forked from the official Home Assistant Core Proxmox VE
integration.

- Repository: `https://github.com/home-assistant/core`
- Tag: `2026.9.1`
- Commit: `fc034572d0216a04ed40a07154394908a594dfed`
- Source path: `homeassistant/components/proxmoxve`
- Test source path: `tests/components/proxmoxve`
- License: Apache License 2.0

The baseline copies every file from the source integration and every upstream
test, fixture, and snapshot. Baseline adaptations are limited to:

- the integration domain (`proxmoxve` to `hubinet_ops`);
- the displayed integration name (`Proxmox VE` to `Hubinet-Ops`);
- the custom integration version (`2026.9.1.0`); and
- domain-qualified translation references; and
- `translations/en.json`, generated from the adapted `strings.json` because
  custom integrations load runtime translations from `translations/`.

The pinned upstream baseline sends `name` when creating VM and LXC snapshots.
Hubinet-Ops intentionally uses the PVE-required `snapname` keyword instead.

The Home Assistant Core baseline remains `2026.9.1` at the commit above.
That upstream version exposes native snapshot creation but not Hubinet-Ops'
operator-facing Snapshot-to-restore select plus explicit Restore button. PR
#12 therefore adds a select platform and QEMU/LXC Restore buttons through
native PVE APIs. Snapshot enumeration remains outside the unchanged normal
upstream-derived coordinator. Restore presentation and orchestration live in
the fork-owned `snapshot_restore.py`; the upstream-derived `button.py` retains
only a narrow setup hook for Restore and Delete and the package-control
availability guards required by Restore exclusion. The package-specific LXC invalidation and Restore
reservation are Hubinet-owned glue around the package extension, not a
replacement for native PVE ownership.

For native snapshot Create, the upstream-derived POST shape remains intact.
Hubinet adds only a narrow post-POST hook in `button.py` that passes the returned
UPID to fork-owned background task observation, terminal notification, and a
confirmed-success signal for the exact guest's snapshot selector. This adds no
snapshot inventory or main-coordinator refresh.

Release 2026.9.1.11 extends the existing fork-owned snapshot adapter and
orchestration with explicit QEMU/LXC Delete through the ordinary native PVE
DELETE endpoint, without force. Delete shares the existing selector and
bounded task observer, signals only the exact selector after confirmed
success, and does not mutate package-manager state. The fork-owned orchestration
also reads existing Update/Autoremove RUNNING records for the narrow LXC
`hubinet-preupd-*` deletion conflict that preserves in-flight reporting; it
adds no package reservation or cleanup-helper reuse. The upstream-derived
button platform's existing setup hook composes the additional buttons;
Create and Restore behavior and the normal coordinator remain unchanged.

The fork-owned package-scan extension is isolated under
`custom_components/hubinet_ops/packages` with a separately deployed forced-
command helper. Its composition changes to the upstream-derived coordinator,
button platform, sensor platform, manifest, strings, and tests are intentionally
small. Upstream remains authoritative for discovery, identity, state, native
entities, and PVE API operations.

The copied upstream tests change only module paths, domain values, snapshot
platform values, and the fixture that enables loading a custom integration.
Fork-owned package-scan tests are separate additions.

Guided enrollment is an intentional fork-owned config-flow divergence. The
upstream-compatible manual credential path and its advanced reauth and
reconfigure behavior remain present. Guided entries instead use one enrollment
pipeline for fresh setup, reauth, and atomic re-enrollment. The guided path adds
a fixed least-privilege PVE identity, release-pinned root bootstrap, in-memory
SSH trust stored in config-entry data, authenticated helper probe, and
package-node gating. An advanced host change clears package trust bound to the
old endpoint. Config-entry version 4 migrates only safe, unambiguous legacy
file-based package trust, rejects files containing active OpenSSH marker lines,
and never removes the old files.

Release 2026.9.1.13 adds only two setup hooks to the upstream-derived integration
entry module: register the fork-owned targetless Scan All HA action and
provision the two integration-managed blueprints. Scan All reads existing
coordinator/runtime_data and delegates to the unchanged package manager;
blueprint delivery uses off-loop native HA file replacement. Native PVE
behavior, discovery, coordinator, permissions, and package lifecycles are
unchanged. Release 2026.9.1.15 removes the blueprint provisioning hook again;
the per-host automatic Scan option adds one options-flow hook to the
upstream-derived `config_flow.py` and one setup call in the entry module.

Release 2026.9.1.14 (owner-accepted Easy Update card and action) adds one more
setup hook to the upstream-derived entry module: `async_setup` also delivers the
fork-owned dashboard card from `custom_components/hubinet_ops/frontend/` through
`hass.http.async_register_static_paths` and `frontend.add_extra_js_url`. The
manifest lists `frontend` and `http` only as `after_dependencies` for ordering. The
`hubinet_ops.easy_update` action and its bounded YOLO continuation live in the
fork-owned `services.py` and `easy_update.py`; they read the device registry,
existing coordinator/runtime data, and call only existing `PackageManager` entry
points. Upstream-derived entities, device identifiers, coordinator, buttons,
sensors, permissions, and native PVE behavior are unchanged.

Release 2026.9.1.15 adds an owner-approved divergence to the upstream-derived
coordinator. Each refresh read (nodes, per-node QEMU, LXC, storage, and the
latest vzdump task) is retried once after a short delay when it fails with an
HTTP 5xx or a read timeout, never for 4xx. When a node's storage or backup read
still fails, that node keeps its previous storage and backup values for that
refresh instead of failing the whole host; node, QEMU, and LXC list failures
still fail the refresh. The setup probe (`nodes` and `access/permissions` in
`_init_proxmox`) is unchanged upstream code without this retry: a transient 5xx
or read timeout there already ends in Home Assistant's own setup retry. Refresh
read failures now report the failed request and
Proxmox error (`api_read_failed`) instead of upstream's `no_nodes_found`. The
change exists because a live LXC Restore made Proxmox answer slowly or with an
error and upstream then marked every entity of the host unavailable.

Release 2026.9.1.16 (owner decision) changes one more line of the
upstream-derived coordinator's setup: a `requests` `ConnectionError` during the
setup probe raises `UpdateFailed` (`cannot_connect`), as the refresh already
does, instead of upstream's permanent `ConfigEntryError`. Home Assistant then
keeps the entry in `SETUP_RETRY` and retries setup itself; upstream left the
entry failed until a manual reload after one dropped connection. The same
applies to a TLS handshake cut off by the peer: a `requests` `SSLError` whose
exception chain contains an `ssl.SSLEOFError` object (matched by type, not by
message) raises `UpdateFailed` (`cannot_connect`). Certificate verification
and every other `SSLError` stay upstream's permanent `ConfigEntryError`.
`ConnectTimeout`, authentication, permission, and node 4xx handling are
unchanged.

Residual upstream risk, deliberately not changed: with password (ticket)
authentication, proxmoxer reports an HTTP 5xx from `/access/ticket` as
`AuthenticationError`, which upstream maps to reauth. Telling it apart would
need fragile matching of proxmoxer internals; guided enrollment uses an API
token, which does not request a ticket.
