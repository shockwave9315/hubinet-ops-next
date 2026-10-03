# Status

Status date: 2026-10-03

## Current baseline

- Upstream: Home Assistant Core tag `2026.9.1`, commit
  `fc034572d0216a04ed40a07154394908a594dfed`.
- Baseline tag: `baseline-ha-2026.9.1`.
- Runtime after merge: a domain-isolated custom-integration fork of Home Assistant
  Core `proxmoxve`, plus the package scan, review, update, and LXC Health
  subsystem, guided fresh-install enrollment, native snapshot Restore, and
  native snapshot Create observation and explicit native Delete documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Integration after merge: `2026.9.1.17`, helper v5, protocol v1.
- Latest tagged release: `2026.9.1.15` (merged PR #20, main commit
  `b865968`); the owner reported the post-merge live tests passed.
- Starting merged baseline for 2026.9.1.16: main `b865968`.
- `2026.9.1.16` merged in PR #21 (main `0409d76`), not yet tagged.
- Starting merged baseline for 2026.9.1.17: main `0409d76`.
- Git history is authoritative for the eventual feature merge SHA.

## Merged

- Clean upstream-derived Proxmox VE integration baseline.
- Reproducible local development and Home Assistant test environment.
- Project-memory foundation defining product, architecture, status,
  provenance, development, and agent responsibilities.
- Manual LXC pending-package scan with no package install/remove/upgrade,
  using AsyncSSH transport, a root-owned forced-command helper, ephemeral
  state, bounded summary entities, and package-specific concurrency.
- Package review: ephemeral scan-token confirmation on the existing
  `PackageScanRecord` through the native Review and Approve operator buttons.
  (Historical: the response-only actions `hubinet_ops.get_package_plan` and
  `hubinet_ops.confirm_package_review` and their supported-feature bit were
  removed in 2026.9.1.15.)
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

Historical record: the blueprints and Mushroom example below were removed in
2026.9.1.15.

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

Historical record: blueprint provisioning below was removed in 2026.9.1.15;
Scan All remains.

- Architecture: **ACCEPTED BY OWNER BEFORE IMPLEMENTATION**, as explicitly
  directed for this task; see [ARCHITECTURE.md](ARCHITECTURE.md).
- Implementation: **MERGED IN PR #18 / RELEASED AS 2026.9.1.13** (main
  `3bc525c`). Pre-implementation documentation checkpoint: `ec877c8`.
- Blueprint sources ship inside the HACS-installed integration package. Setup
  and reload synchronize only the two owned files off-loop with atomic rename,
  skip identical bytes, reset changed blueprint caches, and log delivery errors
  without disabling native entities. User instances are never created/enabled.
- The Polish Scan blueprint has no selector and calls only
  `hubinet_ops.scan_all_packages`; one automation considers package-node LXCs
  across loaded configured entries. Existing manager rejection and concurrency
  rules isolate stopped/busy targets and remain authoritative.
- The one-click Update runtime YAML remains byte-identical to 2026.9.1.12;
  its blueprint-facing labels/descriptions are Polish. Mushroom is unchanged.
- Validation: **50 focused delivery/Scan All/Easy UX tests passed**; full
  `scripts/test.sh`: **860 tests and 213 snapshots passed**. Ruff, EN/PL parity,
  release/helper SHA/protocol checks, ShellCheck, shell syntax, and
  `git diff --check` passed.
- Known delivery residual: old manually imported 2026.9.1.12 copies may appear
  as duplicates until removed by the user. They are not searched or migrated.
- Scope: integration-shipped/provisioned Polish blueprints and one targetless
  Scan All action over existing loaded-entry PackageManagers.
- Package backend delta: **NONE**; helper v5/protocol v1 unchanged.
- Starting merged baseline: PR #17 / `2026.9.1.12`, main
  `8852a21cc2ba49c109a9d85863765253e3e6f39c`.

## 2026.9.1.14 Easy Update card and action (Variant C)

- Architecture: **ACCEPTED BY OWNER BEFORE IMPLEMENTATION on 2026-10-03**;
  see [ARCHITECTURE.md](ARCHITECTURE.md#easy-update-card-and-action-variant-c-20269114).
  The decision explicitly supersedes the earlier "no custom frontend / no custom
  card / no custom target resolver" statements for this card and action only.
- Implementation: **MERGED IN PR #19 / RELEASED AS 2026.9.1.14** and live-tested
  by the owner (card picker, editor, states). Pre-implementation
  documentation checkpoint: `7d38bf8`. Implemented in the accepted order: the
  action without YOLO, the YOLO continuation, frontend delivery, then the card.
- One recorded refinement of the checkpoint: the manifest lists `frontend` and
  `http` as `after_dependencies` instead of hard dependencies. Both are
  stage-0 default integrations; a hard dependency would only force the
  frontend package into every test without changing runtime behavior.
- Maintainer local validation on CT112: baseline `main` `3bc525c` ran 860
  tests with 858 passed and 2 Restore failures; PR #19 at `f1fb856` ran **900
  Python tests, 900 passed**, plus **13/13 Node card tests**, and the full
  `scripts/test.sh` passed. The two baseline Restore failures did not reproduce
  on the PR and are classified as existing Restore timing flakiness, not a
  Variant C regression.
- Cloud-container validation (secondary): the same 40 new Python and 13 card
  tests passed; 45 existing background-task timing assertions in unchanged
  Restore/Snapshot Delete code failed identically on untouched `3bc525c` there.
  Ruff, translation/release parity, ShellCheck, and a headless-Chromium card
  smoke test passed.
- Final-review fix-set: **two confirmed findings closed**. (B, P2) Easy Update
  start and the YOLO follower now stop when the latest coordinator refresh
  failed (`last_update_success` false), like native entities, the blueprint, and
  Scan All; start is refused with `easy_update_not_running` before any review.
  (A, P3) For a non-admin user the action requires `POLICY_CONTROL` on the
  target's existing Update button, plus the Autoremove button when
  `autoremove` is true, resolved by stable unique ID, before any review or
  Update; system context and administrators are unchanged. Five regression
  tests added (905 Python tests total). Cloud-container run after the fix:
  860 passed, the same 45 container-only baseline failures; 13/13 Node card
  tests; Ruff and `git diff --check` passed.
- Live Home Assistant validation: the owner live-tested the released card
  (card picker, editor, states); that live run also surfaced the Restore
  refresh failure fixed in 2026.9.1.15. A live Easy Update with and without
  YOLO was **not performed**; it moves to the 2026.9.1.15 live validation.
- Accepted scope: the `hubinet_ops.easy_update` action with an exact `device_id`
  target, synchronous confirm-and-start over existing `PackageManager` entry
  points, an optional bounded YOLO Autoremove continuation with the unchanged
  blueprint semantics (same successful Update, Health no longer running, Health
  result not a gate, fresh cleanup evidence), integration-shipped frontend
  delivery without a manual dashboard resource, and the Hubinet-Ops Easy Update
  card configured by one LXC device.
- Package backend delta: **NONE**; `packages/*`, helper v5, protocol v1, and
  Scan All are unchanged. The One-click Update blueprint stayed as an advanced
  alternative (removed in 2026.9.1.15).

## 2026.9.1.15 cleanup and refresh resilience

- Architecture: **ACCEPTED BY OWNER BEFORE IMPLEMENTATION on 2026-10-03**; see
  [ARCHITECTURE.md](ARCHITECTURE.md#cleanup-and-refresh-resilience-20269115).
- Implementation: **MERGED IN PR #20**. Documentation checkpoint: `8c024d0`.
- Scope: remove the One-click Update script blueprint and Mushroom example,
  show "no current data from Proxmox" in the Easy Update card, and make one
  slow or failing Proxmox read during Restore no longer take every entity of
  the host offline. Package backend, helper, and protocol are unchanged.

## 2026.9.1.15 automatic Scan option and LXC card

- Architecture: **ACCEPTED BY OWNER BEFORE IMPLEMENTATION on 2026-10-03**; see
  [ARCHITECTURE.md](ARCHITECTURE.md#automatic-scan-option-and-lxc-card-20269115).
- Implementation: **MERGED IN PR #20 / RELEASED AS 2026.9.1.15**; the owner
  reported the post-merge live tests passed. Contents: the
  options flow and daily trigger (`auto_scan.py`), and the LXC card
  (`frontend/hubinet-ops-lxc-card.js`, `frontend/lxc-card-logic.js`) with Node
  logic tests and a browser smoke test of second-tap arming.
  The Scan blueprint, `blueprint_delivery.py`, and their tests are removed.
  Owner-requested cleanup: the `get_package_plan` and `confirm_package_review`
  actions and the `PackageReviewEntityFeature` bit are removed; the Easy
  Update picker now lists devices with the package Update button
  (`ButtonDeviceClass.UPDATE`). LXC card confirmation is bound to the exact
  action and target (selected snapshot identity from `selected_snapshot`).
- Scope: per-host daily automatic Scan in the integration options (replacing the
  Scan blueprint), and the Hubinet-Ops LXC card from the approved mockup with
  second-tap confirmation for Stop, Restart, Restore, and Delete.

## 2026.9.1.16 setup connection resilience

- Architecture: **ACCEPTED BY OWNER BEFORE IMPLEMENTATION on 2026-10-03**; see
  [ARCHITECTURE.md](ARCHITECTURE.md#setup-connection-resilience-20269116).
- Implementation: **MERGED IN PR #21**. Documentation checkpoint:
  `011d735`. `coordinator.py` maps a setup-time `ConnectionError`, and a
  `SSLError` caused by `ssl.SSLEOFError` (TLS handshake cut off), to
  `UpdateFailed`; certificate verification and other TLS errors stay
  permanent. Tests cover the permissions and nodes probes (`SETUP_RETRY`),
  certificate and other TLS errors (`SETUP_ERROR`), and recovery to `LOADED`
  on Home Assistant's retry for both a dropped connection and a TLS EOF.
- Scope: a setup-time transport drop (`ConnectionError`, abrupt TLS EOF)
  retries setup (`SETUP_RETRY`) instead of failing the entry permanently.
  Owner-approved divergence from upstream, recorded in UPSTREAM.md. Password
  `/access/ticket` 5xx reported as `AuthenticationError` is a known residual
  upstream risk, not changed.

## 2026.9.1.17 VM card, mini cards, and stat history

- Architecture: **ACCEPTED BY OWNER BEFORE IMPLEMENTATION on 2026-10-03** (from
  the approved mockup); see
  [ARCHITECTURE.md](ARCHITECTURE.md#vm-card-mini-cards-and-stat-history-20269117).
- Implementation: **NOT STARTED** at the documentation checkpoint.
- Scope: the VM card and the LXC/VM mini cards on one shared implementation,
  and tapping a stat tile to open the native history; frontend only. Pause,
  Resume, and `qmpstatus` are deferred.

## Next

Tag `2026.9.1.16` (main `0409d76`); after this PR merges, tag and release
`2026.9.1.17` and validate the new cards live.

## Explicitly not started

- Tags and releases of `2026.9.1.16` and `2026.9.1.17`.
- Pause and Resume in the VM card, and reading `qmpstatus`.
