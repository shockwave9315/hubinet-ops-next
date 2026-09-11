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
only a narrow setup hook and the package-control availability guards required
by Restore exclusion. The package-specific LXC invalidation and Restore
reservation are Hubinet-owned glue around the package extension, not a
replacement for native PVE ownership.

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
