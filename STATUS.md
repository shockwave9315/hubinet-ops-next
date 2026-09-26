# Status

Status date: 2026-09-26

## Current baseline

- Upstream: Home Assistant Core tag `2026.9.1`, commit
  `fc034572d0216a04ed40a07154394908a594dfed`.
- Baseline tag: `baseline-ha-2026.9.1`.
- Runtime after merge: a domain-isolated custom-integration fork of Home Assistant
  Core `proxmoxve`, plus the package scan, review, update, and LXC Health
  subsystem, guided fresh-install enrollment, native snapshot Restore, and
  native snapshot Create observation and explicit native Delete documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Integration after merge: `2026.9.1.12`, helper v5, protocol v1.
- Latest tagged release: `2026.9.1.11`.
- Starting merged baseline: release `2026.9.1.11`, merged PR #16, main commit
  `6e3c1735f2635453a7e21675407ea2ff7438c54a`.
- Git history is authoritative for the eventual feature merge SHA.
- Feature validation: **844 tests and 213 snapshots passed**; repository Ruff,
  translation and release-parity checks, helper SHA/protocol consistency,
  ShellCheck, and shell syntax passed.

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
- Implementation: **MERGED IN RELEASE 2026.9.1.9**.
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
- Implementation: **MERGED IN PR #15 / RELEASED AS 2026.9.1.10**. Several
  targeted reviews led to lifecycle, evidence, and availability corrections.
  A subsequent full independent architecture review judged the architecture
  sound and required only small fixes, applied in a final correction: a
  reinst-required half-installed package, which `dpkg --audit` lists only
  under its "in a mess" section, is recognized as persistent interrupted dpkg
  state. Later adjudication preserved an established dpkg `FAILED` over
  reboot-probe uncertainty and narrowed dpkg `FAILED` evidence to
  half-installed only: half-configured is `pending` (`UNKNOWN`), because apt
  legitimately leaves deconfigured packages half-configured between successful
  dpkg runs.
- Accepted scope: generic point-in-time OS/package Health for package-eligible
  LXCs, manually runnable and automatically run immediately after a successful
  Update or Autoremove, through one new helper v5 `check_health` operation
  under unchanged protocol v1. `healthy`/`degraded`/`failed`/native `unknown`
  classification, no notifications, no systemd/CPU/RAM/uptime or
  application-specific checks, and no change to Update/Autoremove/Restore
  safety semantics.
- The post-Update/post-Autoremove hand-off claims Health `RUNNING` *before*
  publishing the terminal `SUCCESS` (re-entrancy-safe against eager HA
  listeners), starts no Health from the unload/reload cancellation path, uses
  its own tight 60s/20s/10s/90s timeout model instead of scan-sized bounds,
  and owns its background-task coroutine explicitly on both the manual and
  post-operation paths. `begin_restore()` and existing Update/Autoremove
  safety checks are unchanged.
- Release state after merge: integration `2026.9.1.10`, helper v5, protocol v1;
  tagged and released as `2026.9.1.10`.
- Validation: **738 tests and 207 snapshots passed**; repository Ruff,
  translation and release-parity checks, helper SHA/protocol consistency,
  ShellCheck, and shell syntax passed.

## 2026.9.1.11 native snapshot Delete

- Architecture: **ACCEPTED BY MAINTAINER on 2026-09-26**, before runtime
  implementation; see [ARCHITECTURE.md](ARCHITECTURE.md#native-snapshot-delete).
- Implementation: **MERGED IN PR #16 / RELEASED AS 2026.9.1.11**. The pre-implementation
  documentation checkpoint is commit `246baa7`.
- Independent review correction: architecture **ACCEPTED CONDITIONALLY BY
  MAINTAINER / CONDITION CONFIRMED**; implementation **MERGED IN PR #16**.
  Pre-runtime documentation checkpoint: `636136e`. Four
  reproductions establish false retained-snapshot reporting when generic Delete
  removes an active Update/Autoremove safety snapshot. The accepted correction
  uses only existing RUNNING records for the exact LXC target and
  `hubinet-preupd-*` choice, before acceptance and after fresh listing.
- Validation: **233 focused tests and 213 snapshots passed**; full suite
  **810 tests and 213 snapshots passed**. The correction adds 22 regressions
  covering both mutation outcomes, both package operations, the post-listing
  conflict recheck, EN/PL results, and unrestricted owner choices outside the
  narrow conflict. Translation/release parity,
  helper SHA/protocol consistency, Ruff, ShellCheck, and shell syntax passed.
- Accepted scope: one explicit Delete button using the existing exact selector,
  fresh native validation, ordinary QEMU/LXC DELETE, bounded background UPID
  observation, localized results, and success-only exact selector refresh.
  External/manual snapshots are equally deletable; the external naming warning
  is warning-only. The sole package conflict is the active LXC transaction
  described above.
  Package truth, package cleanup protection, helper v5, and protocol v1 stay
  unchanged.
- Release target: integration `2026.9.1.11`, helper v5, protocol v1.

## 2026.9.1.12 Easy Update UX

- Architecture: **ACCEPTED BY MAINTAINER BEFORE IMPLEMENTATION**; optional
  HA YAML composition over existing entities and entity actions only.
- Implementation: **MERGED IN PR #17 / RELEASED AS 2026.9.1.12**. Documentation checkpoint:
  `9f87618`. Automatic Scan automation blueprint, one-click Update script
  blueprint with opt-in Autoremove, Mushroom example, and Easy/YOLO/Manual
  instructions are shipped artifacts, validated with the pinned HA blueprint
  loader and script engine. Both entity-action responses use the actual
  `hubinet_ops` service domain and per-sensor response keys.
- Backend lifecycle delta: **NONE**. Release metadata only may change under
  `custom_components/hubinet_ops`; helper v5/protocol v1 remain unchanged.
- Validation: **34 focused Easy UX tests passed**; full `scripts/test.sh`
  **844 tests and 213 snapshots passed**. Ruff, translation/release parity,
  helper SHA/protocol consistency, shell checks, and `git diff --check` passed.
  An initial full run hit three existing Snapshot Delete background-task
  assertion timing failures; the affected 14-test group, all 810 existing
  tests alone, and the final full run passed. Snapshot code/tests are unchanged.
- Release target: `2026.9.1.12`.

## 2026.9.1.13 Easy UX delivery + Scan All

- Architecture: **ACCEPTED BY OWNER BEFORE IMPLEMENTATION**, as explicitly
  directed for this task; see [ARCHITECTURE.md](ARCHITECTURE.md).
- Implementation: **NOT STARTED**. Target: `2026.9.1.13`.
- Scope: integration-shipped/provisioned Polish blueprints and one targetless
  Scan All action over existing loaded-entry PackageManagers.
- Package backend delta: **NONE**; helper v5/protocol v1 unchanged.
- Starting merged baseline: PR #17 / `2026.9.1.12`, main
  `8852a21cc2ba49c109a9d85863765253e3e6f39c`.

## Next

Implement the accepted 2026.9.1.13 correction. No merge, tag, or release is
performed by this task.

## Explicitly not started

- Tagging and publishing `2026.9.1.13` await maintainer review and merge.
