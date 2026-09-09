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

The fork-owned package-scan extension is isolated under
`custom_components/hubinet_ops/packages` with a separately deployed forced-
command helper. Its composition changes to the upstream-derived coordinator,
button platform, sensor platform, manifest, strings, and tests are intentionally
small. Upstream remains authoritative for discovery, identity, state, native
entities, and PVE API operations.

The copied upstream tests change only module paths, domain values, snapshot
platform values, and the fixture that enables loading a custom integration.
Fork-owned package-scan tests are separate additions.

Guided fresh-install enrollment is an intentional fork-owned config-flow
divergence. The upstream-compatible manual credential, reauth, reconfigure,
API validation, discovery, and entity behavior remain present. The guided path
adds a fixed least-privilege PVE identity, release-pinned root bootstrap,
in-memory SSH trust stored in config-entry data, authenticated helper probe,
and package-node gating. Config-entry version 4 migrates only safe,
unambiguous legacy file-based package trust and never removes the old files.
