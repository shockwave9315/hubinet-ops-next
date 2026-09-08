# Package-scan donor map

The donor review was limited to `app/package_scan.py`,
`app/package_scan_host_control.py`, `deploy/hubinet-package-scan-helper.py`, and
their direct package-scan execution tests in the old Hubinet-Ops repository.

## KEEP

- strict `/etc/os-release` parsing and Debian/Ubuntu validation;
- native architecture and installed-package inventory parsing from `dpkg`;
- fail-closed handling of unfinished dpkg states;
- exact parsing and cross-checking of APT `Inst`/`Conf` simulation rows;
- multiarch identity and security-origin classification;
- reboot-required evidence;
- fixed command argument vectors, bounded process time/output, and no shell;
- bounded busy, timeout, metadata-refresh, simulation, and execution failures.

## DROP

- `InventoryAuthority`, package-scan run records, UUID bindings, continuity and
  reconciliation context, persistence, publication, and plan fingerprints;
- approval, update, snapshot, rollback, worker, scheduler, and health logic;
- the legacy HTTP/backend/hostd transport and its configuration;
- cluster-resource discovery and cross-node routing in the helper; the helper
  only verifies the coordinator-supplied node against the PVE-native local
  node identity derived from `/etc/pve/local` and validates the requested
  LXC through fixed `pct` commands;
- all Docker package-update behavior and every helper unrelated to package
  scanning.
