# Status

Status date: 2026-09-13

## Current baseline

- Upstream: Home Assistant Core tag `2026.9.1`, commit
  `fc034572d0216a04ed40a07154394908a594dfed`.
- Baseline tag: `baseline-ha-2026.9.1`.
- Merged runtime: a domain-isolated custom-integration fork of Home Assistant
  Core `proxmoxve`, plus the package scan, review, and update subsystem and
  guided fresh-install enrollment and native snapshot Restore documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Current release: integration `2026.9.1.8`, helper v4, protocol v1.
- Merged baseline commit:
  `c699f3e75ff3c94ae34b22de44a276b8ab9b88a7`.
- Baseline validation: **564 tests and 207 snapshots passed**; repository Ruff
  passed.

## Merged

- Clean upstream-derived Proxmox VE integration baseline.
- Reproducible local development and Home Assistant test environment.
- Project-memory foundation defining product, architecture, status,
  provenance, development, and agent responsibilities.
- Manual LXC pending-package scan with no package install/remove/upgrade,
  using AsyncSSH transport, a root-owned forced-command helper, ephemeral
  state, bounded summary entities, and package-specific concurrency.
- Package review: ephemeral scan-token confirmation on the existing
  `PackageScanRecord`, exposed as the two response-only sensor-platform
  actions `hubinet_ops.get_package_plan` and
  `hubinet_ops.confirm_package_review`, restricted to the package sensor
  via a native supported-feature bit, plus the native Review and Approve
  operator buttons.
- Package Update: execution-time exact-plan gating, one native retained safety
  snapshot, one fixed hardened bare APT upgrade, post-mutation dpkg sanity,
  generic LXC liveness, exact snapshot cleanup, bounded outcome sensor, and
  persistent result notifications.
- Guided fresh-install enrollment: one release-pinned PVE bootstrap command,
  one temporary enrollment paste, pre-entry API/SSH/helper/node validation,
  in-entry SSH trust, and local-node package entity scope.

## Package scan

- Independent read-only architecture audit: **COMPLETE**.
- Package-scan architecture: **ACCEPTED BY MAINTAINER** and documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Package scan: **IMPLEMENTED**.
- Package state is intentionally ephemeral; Home Assistant restart resets it.

## Package review

- Package-review architecture: **ACCEPTED BY MAINTAINER** and documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Package-review implementation: **IMPLEMENTED**.

## Package Update

- Package Update architecture: **ACCEPTED BY MAINTAINER BEFORE IMPLEMENTATION
  on 2026-09-09**; see the
  [maintainer decision record](ARCHITECTURE.md#package-update-maintainer-decision-record).
- Package Update: **IMPLEMENTED**.

## Guided enrollment

- Guided enrollment architecture: **ACCEPTED BY MAINTAINER** and documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Guided enrollment and the version-4 legacy trust migration: **IMPLEMENTED**.
- Guided lifecycle repair after independent review: **IMPLEMENTED IN PR #8**.
  Plain bootstrap reconciles deterministic resources without credential
  rotation; `--reset` and guided re-enrollment atomically recover existing
  entries; package trust failures degrade to native-only operation.

## Helper repair and package cleanup

- Architecture: **ACCEPTED BY MAINTAINER on 2026-09-10**, before runtime
  implementation; see the durable boundary in [ARCHITECTURE.md](ARCHITECTURE.md).
- Helper-upgrade UX: **IMPLEMENTED**. A non-blocking one-shot observation
  reports helper v3 as stale through Repairs with the plain release-pinned
  bootstrap command, while native PVE and supported helper operations continue.
- Explicit package cleanup: **IMPLEMENTED**. Scan and successful Update may
  observe exact unused-package evidence; Autoremove requires successful exact
  presentation, explicit button action, a fresh equal plan, and the existing
  native snapshot/dpkg/liveness safety path.
- Release target: integration `2026.9.1.5`, helper v4, protocol v1.
- Post-audit bounded correction pass: **COMPLETE**.
- Validation: **473 tests and 195 snapshots passed**; repository Ruff passed.

## PR #11: UX truthfulness and localization

- Package evidence truthfulness architecture: **ACCEPTED BY MAINTAINER on
  2026-09-10**; implementation **IMPLEMENTED**. A non-running observation now
  discards current Scan, Review, and cleanup evidence while preserving running
  and terminal mutation attempts.
- Polish and English localization repair: **IMPLEMENTED** across the normal
  user-facing integration surface, including package notifications and tables.
- Guided reinstall recovery UX: **IMPLEMENTED** with the existing `--reset`
  command shown as the secondary recovery path.
- Release target: integration `2026.9.1.6`, helper v4, protocol v1.
- Validation: **495 tests and 195 snapshots passed**; repository Ruff passed.

## PR #12: native snapshot Restore (merged)

- Architecture: **ACCEPTED BY MAINTAINER on 2026-09-11**, before runtime
  implementation; see the durable boundary in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Implementation: **MERGED IN RELEASE 2026.9.1.7**. Native PVE snapshot
  selection and explicit Restore are available for eligible QEMU VMs and LXCs;
  LXC package truth and operation exclusion follow the accepted fail-closed
  boundary.
- Release: integration `2026.9.1.7`, helper v4, protocol v1 unchanged.
- Post-implementation red-team result: **PASS AFTER SMALL FIXES**. The focused
  file-boundary, initial-polling, selector-identity, validation, and safety-test
  corrections are included in the merged release.
- Validation: **563 tests and 207 snapshots passed**; repository Ruff,
  translation and release-parity checks, helper SHA/protocol consistency,
  ShellCheck, and shell syntax passed.

## 2026.9.1.8 Restore polling hotfix

- Hotfix: **MERGED / RELEASED** within the accepted PR #12
  architecture.
- Restore no longer forces an immediate full coordinator refresh; native guest
  state converges through the normal coordinator poll.
- Snapshot selector initial, periodic, and manual updates use Home Assistant's
  native entity-platform concurrency limit of three.
- Release: integration `2026.9.1.8`, helper v4, protocol v1 unchanged.
- Validation: **564 tests and 207 snapshots passed**; repository Ruff,
  translation and release-parity checks, helper SHA/protocol consistency,
  ShellCheck, and shell syntax passed.

## 2026.9.1.9 native snapshot Create observation

- Architecture: **ACCEPTED BY MAINTAINER on 2026-09-13**; see the durable
  boundary in [ARCHITECTURE.md](ARCHITECTURE.md).
- Implementation: **IMPLEMENTED / READY FOR REVIEW**.
- Accepted behavior: observe the exact native Create UPID in the background,
  publish a terminal result notification, and refresh only the exact guest's
  snapshot selector after confirmed success. The main coordinator remains
  uninvolved.
- Accepted residuals: an already-running selector update can rarely absorb the
  targeted refresh and then self-heal through normal polling within about 300
  seconds; an ambiguous upstream POST failure without a returned UPID retains
  existing button-error behavior and relies on normal selector polling.
- Release target: integration `2026.9.1.9`, helper v4, protocol v1 unchanged.
- Validation: **584 tests and 207 snapshots passed**; repository Ruff,
  translation and release-parity checks, helper SHA/protocol consistency,
  ShellCheck, and shell syntax passed.

## 2026.9.1.10 LXC Health

- Architecture: **ACCEPTED BY MAINTAINER on 2026-09-13**, before runtime
  implementation; see the durable boundary in
  [ARCHITECTURE.md](ARCHITECTURE.md#lxc-health).
- Implementation: **IMPLEMENTED / READY FOR REVIEW**.
- Accepted scope: generic point-in-time OS/package Health for package-eligible
  LXCs, manually runnable and automatically run immediately after a
  successful Update or Autoremove, through one new helper v5 `check_health`
  operation under unchanged protocol v1. `healthy`/`degraded`/`failed`/native
  `unknown` classification, no notifications, no systemd/CPU/RAM/uptime or
  application-specific checks, and no change to Update/Autoremove/Restore
  safety semantics.
- The atomic post-Update/post-Autoremove hand-off, Health's own busy model,
  and its identity-guarded evidence invalidation are implemented exactly as
  accepted; `begin_restore()` and existing Update/Autoremove safety checks
  are unchanged.
- Release: integration `2026.9.1.10`, helper v5, protocol v1 unchanged.
- Validation: **681 tests and 207 snapshots passed**; repository Ruff,
  translation and release-parity checks, helper SHA/protocol consistency,
  ShellCheck, and shell syntax passed.

## Next

Review and release the accepted `2026.9.1.10` LXC Health release.

## Explicitly not started

- Nothing currently accepted and undesigned; see [Next](#next) for the item
  awaiting review and release.
