# Status

Status date: 2026-10-04

## Current baseline

- Upstream: Home Assistant Core tag `2026.9.1`, commit
  `fc034572d0216a04ed40a07154394908a594dfed`.
- Baseline tag: `baseline-ha-2026.9.1`.
- Runtime after merge: a domain-isolated custom-integration fork of Home Assistant
  Core `proxmoxve`, plus the package scan, review, update, and LXC Health
  subsystem, guided fresh-install enrollment, native snapshot Restore, and
  native snapshot Create observation and explicit native Delete documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Planned development release for the whole A/B/C/D stage: `2026.9.1.21`,
  helper v5, protocol v1. No .21 tag or release has been created.
- Latest final tagged release: `2026.9.1.20` (merged PR #25, main `b38b950`).
  `2026.9.1.15` (PR #20, `b865968`): the owner reported the post-merge live
  tests passed.
- Starting merged baseline for 2026.9.1.16: main `b865968`.
- `2026.9.1.16` merged in PR #21 (main `0409d76`), not yet tagged.
- Starting merged baseline for 2026.9.1.17: main `0409d76`.
- Starting merged baseline for 2026.9.1.18: main `e7c03e6`.
- Starting merged baseline for 2026.9.1.19: main `05b0e20`.
- Starting merged baseline for 2026.9.1.20: main `d4eb2eb`.
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

## Checkpoint A: one-shot Update without a snapshot

- Execution plan A/B/C/D: **ACCEPTED BY OWNER on 2026-10-04**.
  A was the first authorized implementation; its accepted contract is in
  [ARCHITECTURE.md](ARCHITECTURE.md#checkpoint-a-explicit-one-shot-package-update-without-a-snapshot).
- Checkpoint A implementation: **IMPLEMENTED / READY FOR OWNER REVIEW** on
  `feat/checkpoint-a-skip-snapshot`, based on main `b38b950` after PR #25.
  Pre-runtime documentation acceptance checkpoint: `1988eaa`.
- Default/native Update and all Autoremove remain snapshot-required; the
  explicit one-shot Easy Update exception changes only snapshot work.
- `skip_snapshot=False` is the service/manager default and explicit native
  press value. `True` skips permission gating, listing, warnings, naming,
  Create/confirmation, and cleanup, then uses the existing guarded Update,
  liveness, cleanup observation, Health, and invalidation path.
- The native Update button also exists without `VM.Snapshot`, but remains
  unavailable; its `snapshot_permission` capability attribute separately gates
  the normal Easy action. `snapshot_skipped` and EN/PL notifications report
  actual attempt truth, including failure/cancellation before mutation and
  pruning. The YOLO continuation never passes skip to Autoremove.
- Validation: **188 targeted Python tests and 72 Node tests passed**; full
  `scripts/test.sh`: **981 Python tests, 72 Node tests, 213 snapshots passed**,
  plus repository Ruff. `git diff --check` passed. Targeted self-review of the
  full A change found no unresolved scope, runtime, or architecture issue.
- Live PVE execution of A has not been performed; owner review/live validation
  remains outstanding. The A checkpoint itself changed no release metadata.

## Current A/B/C/D stage (2026.9.1.21)

- Owner authorized continuing the existing `feat/checkpoint-a-skip-snapshot`
  branch, a version-only commit, and one Draft PR against `main` for the whole
  stage. The planned version remains .21 through A/B/C/D; no tag, release,
  or merge is authorized.
- A: **DONE**, HEAD after A `49a12c1`, validation recorded above.
- B architecture: **ACCEPTED BY OWNER BEFORE RUNTIME IMPLEMENTATION**; see
  [the accepted contract](ARCHITECTURE.md#checkpoint-b-native-snapshot-create-running).
  Implementation: **DONE / READY FOR OWNER REVIEW**. Pre-runtime
  documentation checkpoint: `35a82ac`; runtime/tests: `8f679cf`.
- Draft PR: [#26](https://github.com/shockwave9315/hubinet-ops-next/pull/26),
  against `main`, on the existing branch. It covers the whole A/B/C/D stage and
  remains **DRAFT**. Version-only commit: `cc5b1e2`.
- B's native Create entity owns the exact existing observation task and
  publishes `snapshot_create_running` at launch and task completion.
  Restore/Delete read the same live entity through HA's existing registry and
  button component, before acceptance and after fresh validation. No new
  observer, registry, store, timer, coordinator, or lifecycle is introduced.
- Full VM/LXC cards reconstruct the spinner/label and snapshot-only blocking
  from the backend fact, including remount; power controls retain their rules.
  Success, failure, uncertainty, cancellation, eager completion, POST failure,
  and observation-launch failure are covered. Notifications and success-only
  selector refresh retain their existing semantics.
- B validation: **174 targeted Python tests, 80 Node tests, and 56 snapshots
  passed**, plus repository Ruff. Targeted self-review: **PASS**, no unresolved
  B finding. A regression: **188 Python tests and 80 Node tests passed**.
- Final full `scripts/test.sh`: **1004 Python tests, 80 Node tests, and 213
  snapshots passed**, plus repository Ruff (56.18s Python run).
  `git diff --check`, canonical EN parity and PL exception-key parity passed.
  The unchanged observer/notification functions and A package-button classes
  were checked structurally against A's HEAD. No Lovelace Resources mechanism
  or C/D runtime/layout change is included.
- Live PVE and owner dashboard validation of B have not been performed. Existing
  bounded-observation uncertainty and ephemeral HA ownership remain accepted
  limits; there is no PVE-success claim on UNCERTAIN. Optional test-file Ruff
  still reports the same 13 pre-existing diagnostics in upstream-derived
  `test_button.py`; mandatory repository Ruff passes and B adds none.
- C/D architecture: **ACCEPTED BY OWNER BEFORE RUNTIME IMPLEMENTATION on
  2026-10-04**, as one joint stage after owner review and acceptance of A and
  B; see the accepted contracts for
  [C](ARCHITECTURE.md#checkpoint-c-full-lxc-skip-action-and-responsive-guest-cards)
  and
  [D](ARCHITECTURE.md#checkpoint-d-mini-width-and-optional-vm-guest-memory).
  Implementation: **IN PROGRESS**.
- PR #25 Lovelace Resources mechanism remains unchanged.

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
- Implementation: **MERGED IN PR #22 / RELEASED AS 2026.9.1.17**. Documentation
  checkpoint: `b276811`. `frontend/guest-card-logic.js` (renamed from `lxc-card-logic.js`)
  holds LXC and VM role tables; `frontend/hubinet-ops-guest-cards.js` (renamed
  from `hubinet-ops-lxc-card.js`) registers the four card types on one element.
  Node logic and element tests cover VM power and confirmation, More, mini
  cards, history taps, picker registration, and saved `compact: true` cards; a
  Chromium smoke test drove all four cards. Live testing then found the
  load-order race fixed in 2026.9.1.18.
- Scope: the VM card and the LXC/VM mini cards on one shared implementation,
  and tapping a stat tile to open the native history; frontend only. Pause,
  Resume, and `qmpstatus` are deferred.

## 2026.9.1.18 card load-order fix

- Defect found in live testing of 2026.9.1.17 (also present since 2026.9.1.14):
  cards intermittently "Custom element doesn't exist" on the dashboard and an
  endless spinner in the card picker, differing per device and cache. Cause:
  Home Assistant imports the card module in parallel with its app bundle, whose
  scoped custom-element registry polyfill replaces `window.customElements`;
  cards defined before it are invisible to Home Assistant.
- Implementation: **MERGED IN PR #23 / TAGGED AS 2026.9.1.18**. Reproduced in
  a real Home Assistant (frontend 20260826.6) by delaying its app bundle; the
  released module waited until `<home-assistant>` was defined, with a 10-second
  fallback. The initial Node test failed on the previous module and passed on
  this release. Its new version also changed the module URL for cached clients.

## 2026.9.1.19 card slow-start correction

- Tester report: the same saved VM card works in desktop and mobile browsers,
  but Android Companion App reports `Custom element doesn't exist:
  hubinet-ops-vm-card`, including after app reinstallation and token reset.
- Owner reproduction: after installing 2026.9.1.18, laptop and phone worked
  while connected through the home VPN; disconnecting that VPN while away
  from home then made all cards fail in the phone app. VPN disconnection is
  a required live-validation case. The delayed-start lab does not simulate
  that network transition or establish whether the app changed HA URLs.
- The 2026.9.1.18 fallback reproduced the same race when the app started after
  ten seconds: all five cards were defined in the native registry before the
  app installed its polyfill, leaving none visible to Home Assistant.
- Implementation: **MERGED IN PR #24 / RELEASED AS 2026.9.1.19**, within the
  existing frontend delivery. The module waits for `home-assistant` with no
  timeout fallback;
  the polyfill's native stand-in resolves an already-pending native wait.
  No delivery, card configuration, backend, helper, or protocol change.
- Node regressions exercise a slow app, a registry installed after ten seconds,
  replacement during a native wait, and an app already ready before importing
  the cards, including duplicate resource imports.
- Validation: full `scripts/test.sh` passed with **877 Python tests, 66 Node
  tests, and 213 snapshots**; Ruff and `git diff --check` passed. The two
  slow-start regressions failed on the unchanged 2026.9.1.18 module and passed
  after removing the fallback.
- After the 2026.9.1.19 metadata bump,
  `scripts/test.sh -k 'bootstrap or frontend_delivery or config_flow'` passed
  **110 Python tests and all 66 Node tests**, plus Ruff. The release pins and
  versioned frontend delivery remain consistent.
- The restarted Docker instance served all four card modules with
  `?v=2026.9.1.19` and HTTP 200; the saved VM and all five picker previews
  passed the 14-second startup/V2 bridge case again with this version.
- Runtime validation: official Home Assistant Container **2026.9.4**, frontend
  **20260826.7**, with the integration mounted read-only and one saved VM tile
  opened by fresh Chromium contexts. Unchanged 2026.9.1.18 worked at normal
  desktop startup; delaying the real app bundle by **14 seconds** with the
  Android V2 authentication/message bridge reproduced the exact VM element
  error and five indefinitely loading picker previews with no card names.
  All four frontend modules returned HTTP 200: delivery succeeded, but the
  app registry lost all five definitions.
- The corrected module registered all five elements and rendered the saved VM
  tile plus all five native picker previews with the same delay and V2 bridge.
  It also passed the V1 bridge, mobile browser, and delayed desktop cases.
  All six runtime comparisons met their expected results, with no console or
  page errors. The lab has no Proxmox host: card creation is verified with its
  native choose-device state, not live guest data or power operations.
- Live confirmation on the owner's Android app: **EXTERNAL-URL FAILURE
  PERSISTS ON 2026.9.1.19**. The runtime lab used Chromium mobile emulation and
  HA's actual external-app frontend path, not an Android WebView. The reproduction proves
  the timeout defect, not the tester's exact cause. The isolated container and
  evidence are under ignored `.dev/frontend-lab-2026.9.4`; the standard test
  environment remains pinned to Core 2026.9.1.
- Release metadata is `2026.9.1.19` in the manifest, integration constant, and
  bootstrap pin. The native extra-module URL and its dependency queries change
  to `?v=2026.9.1.19` so clients fetch the corrected code. The owner merged
  PR #24, tagged the version, and published the release on 2026-10-03.

## Android external-URL card investigation

- Native Lovelace resource delivery architecture: **ACCEPTED BY OWNER on
  2026-10-04**, before implementation. The owner explicitly requested replacing
  `add_extra_js_url` with native Lovelace Resources of type `module`, loaded
  when the dashboard starts. See [ARCHITECTURE.md](ARCHITECTURE.md).
  Implementation: **MERGED IN PR #25**; release line
  `2026.9.1.20`. Pre-implementation documentation checkpoint: `fa2dd32`.
  [PR #25](https://github.com/shockwave9315/hubinet-ops-next/pull/25).
- Owner report after installing 2026.9.1.19: the Android app uses an external
  HA URL through Cloudflare Tunnel with client-certificate authentication
  (mTLS). HA itself works, but the saved VM card still reports
  `Custom element doesn't exist: hubinet-ops-vm-card` and Hubinet cards are
  missing from the picker. Connecting the home VPN makes the app switch to
  a local IP, reload, and display the cards. Desktop and phone browsers work;
  the owner's browser Cloudflare access uses a login. The owner confirmed
  that the browser and app use **different domains**. A successful browser
  request therefore does not establish delivery on the app's domain.
- The reported app is **2026.6.5-full**, with Android WebView
  **153.0.8010.36**. App reinstall, token reset, and phone cache clearing have
  already been tried. None establishes the external failure's cause.
- The owner's two probe reports on 2026-10-04 confirm the same Core 2026.9.4,
  Companion 2026.6.5, and WebView 153 on both origins. Externally, all four
  diagnostic GETs returned HTTP 200 JavaScript without redirects, with exact
  2026.9.1.19 SHA-256 hashes; zero card elements or picker types were registered,
  and the parent resource history contained no Hubinet request. Locally, all
  five elements and picker types were present, and the parent recorded all
  four module requests as scripts with HTTP 200. This narrows the investigation
  to automatic startup/import delivery or execution; successful diagnostic
  fetches do not establish the earlier startup responses or their cause.
  Resource timing has a bounded buffer, so the empty external history alone
  is not proof that no module request occurred.
- Investigation: **OWNER VALIDATION PASS** on `fix/cards-external-mtls` for
  native resource delivery; the owner-reported Android/mTLS failure is resolved.
  The P3 resource-removal lifecycle fix below is implemented and checked. The
  historical slow-start race is not an established cause of the external failure.
- Isolated HA Container 2026.9.4 lab: a loopback-only HTTPS reverse proxy
  rejects requests without a valid test client certificate. All four card
  modules returned HTTP 200 and JavaScript MIME types with the certificate;
  the saved VM and all five picker previews passed in a browser and HA's
  V1/V2 external-app frontend paths. The V2 path also passed
  HTTPS -> local HTTP -> HTTPS in one context. These are Chromium tests with
  simulated native bridges, not Cloudflare or Android WebView tests.
- Three controlled lab denials reproduce the missing VM element while HA
  remains usable: denying the root module or Easy Update logic prevents all
  five definitions; denying the guest module leaves only Easy Update
  defined. These injected HTTP 403 responses show a possible failure
  mechanism, not evidence of a Cloudflare denial on the owner's server.
- A standalone read-only diagnostic, [frontend-card-probe.html](tools/frontend-card-probe.html),
  can run in HA's built-in Webpage card with a relative `/local` URL. It reads
  the actual parent app's element registry, picker types, and existing resource
  timing entries, and fetches the four card modules through the selected HA
  origin. It reports HTTP/MIME, redirects, Cloudflare Ray ID when present, and
  SHA-256 against 2026.9.1.19; this initial phase never imports those modules
  or reads auth storage. [Usage](tools/FRONTEND_CARD_PROBE.md) describes the external/local
  comparison. This is a manual test artifact, not a production delivery change.
- Diagnostic version 2 also reads the active document's Hubinet import entries,
  script types, frontend entrypoint paths, modern/legacy flags, and service-worker
  control. It compares them with a separate HTML response fetched for the current
  panel path and detects Rocket Loader markers. It never executes fetched HTML
  or reports raw script bodies, nonce values, or CSP text. Intermediaries can
  affect this GET too; it is not guaranteed origin-server evidence.
- At the owner's explicit request, version 2 also offers a separate force-load
  button: it appends the existing versioned module with a unique probe query
  to the parent HA document after HA is defined. It records the original
  state, current-registry readiness, subsequent definitions/picker types,
  relevant JS errors, script events, and a bounded 20-second observation.
  A `load` event alone does not prove module evaluation completed. One
  in-memory parent-window report survives iframe recreation; a full reload
  clears it. This changes only the open page's registrations, not HA config,
  backend, release code, or permanent frontend delivery.
- All four diagnostic runtime cases passed in the isolated HA 2026.9.4 mTLS
  lab: healthy registration, a denied root module, a denied guest module, and
  readable release-matching source with registration deliberately suppressed.
  The reports distinguish 5/0/1/0 registered cards respectively and preserve
  the parent registry. Copying the complete JSON was checked in each case;
  none contained the lab's auth tokens or password. These are Chromium tests
  with a simulated V2 bridge, not native Android or Cloudflare tests.
- Diagnostic version 2 passed all six runtime cases, including a removed
  HTML import directive and an inert script type. Both new cases reproduce
  the owner's empty parent request history and registry while all four files
  remain readable. The diagnostic correctly distinguishes the missing and
  present-but-inactive launchers, preserves the registry, and copies the report
  without lab auth material, including an injected nonce containing a lab token.
- All nine force-enabled diagnostic cases passed on the same HA/mTLS/V2 lab.
  Force-loading restores five definitions when the launcher is absent/inert,
  initial registration was suppressed, or an initial await is deliberately
  stalled. Root/guest denials remain visible; a deliberately thrown module
  error is captured, and a forced pending await reaches the observation timeout.
  Tests verify parent-only registration, no duplicate picker entries, full
  JSON copying, retained pre-force evidence, and recovery when HA recreates
  the iframe. These controlled faults validate the diagnostic and do not
  demonstrate an Android registry-promise defect.
- The latest stable official Android GitHub release checked on 2026-10-04 is
  [2026.8.4](https://github.com/home-assistant/android/releases/tag/2026.8.4).
  It includes [#7284](https://github.com/home-assistant/android/pull/7284),
  awaiting client-certificate loading, and
  [#7381](https://github.com/home-assistant/android/pull/7381), priming mTLS
  before a cold frontend load. The latter addresses a first WebSocket connection
  failing when it cannot request a client certificate. These are concrete
  reasons to compare the official full APK with 2026.6.5, not proof of the
  owner's card-only cause. The owner reports no newer Play Store build offered;
  that distribution state has not been independently verified.
- The owner updated to **Companion 2026.8.4-24228** and supplied another
  external v1 report at 06:06:15 UTC on 2026-10-04. The same WebView 153 and
  HA 2026.9.4 still have zero definitions/picker types and no recorded Hubinet
  requests. All four diagnostic responses are HTTP 200 JavaScript, now CF
  cache hits, with the same correct 2026.9.1.19 hashes. The app update did
  **NOT** resolve the reported missing cards.
- The owner reports that the v2 force-load button restored the cards in the
  external app. The supplied follow-up snapshot at 06:44:33 UTC already has
  five definitions/picker types and two batches of module script requests;
  it lacks `forceLoad`, so it does not preserve the failed pre-force state.
  The active document has no Hubinet launcher or literal module path, whereas
  the separate HTTP 200 HTML response contains the normal version-19 import.
  Service-worker control is true; no Rocket Loader marker was detected.
  This proves that the current app can render the registered cards and shows
  differing active/fetched bootstrap documents. It does not establish which
  intermediary supplied the original document or an old-registry await defect.
- Native Android testing was attempted with the official
  **2026.6.5-full** APK on an isolated Android 15 emulator. Its bundled
  WebView is **124.0.6367.219**, so it does not match the owner's WebView 153.
  The stock emulator renderer repeatedly crashed with SIGTRAP while loading
  HA's OAuth page over local HTTP. This prevented authenticated card assertions
  or a native mTLS comparison; it is a lab infrastructure failure, not evidence
  of the owner's card-loading cause. The emulator was stopped after preserving
  its userdata and logs. Scripts, private
  test credentials, certificates, and evidence remain under ignored
  `.dev/frontend-lab-2026.9.4`; the pinned development environment is unchanged.

## 2026.9.1.20 native Lovelace resource delivery

- Architecture: **ACCEPTED BY OWNER BEFORE IMPLEMENTATION on 2026-10-04**.
  Implementation: **MERGED IN PR #25**, main `b38b950`.
  Checkpoint: `fa2dd32`.
- Setup serves the existing static directory and uses HA's native resource
  collection to create/update one relative versioned `module` entry. Old
  entries for the exact integration-served relative path are consolidated;
  unrelated resources and dashboard configs are preserved. The setup no
  longer calls `add_extra_js_url`; `lovelace` is a soft ordering dependency.
- Native YAML resource mode remains operator configured, with a precise
  logged URL and a README declaration. No YAML is rewritten or overlaid.
- Validation including the P3 fix: full `scripts/test.sh` passed **893 Python
  tests, 66 Node tests, and 213 snapshots**, plus Ruff and formatting/diff checks.
  Native-collection
  regressions cover release upgrades, stable IDs, duplicate owned URLs,
  unrelated resources, repeated setup, YAML mode, isolated save failures,
  permanent last-host removal, disabled/not-loaded hosts, ignored discovery,
  unload and same-runtime re-add, a host added during collection loading,
  overlapping removals, and isolated cleanup failures.
- Seven checks passed on actual HA Container 2026.9.4 with the local mTLS
  proxy and mobile Chromium: automatic resource creation during HA startup,
  rejection without a client certificate, the no-resource negative control,
  browser, native V1/V2 frontend paths, external -> local -> external switching,
  and a controlled service-worker reload. All healthy dashboard and picker
  runs have five definitions/types with no Hubinet import in the navigation
  HTML. Removing the native resource leaves zero definitions while the source
  still returns HTTP 200; restoring it repairs loading on the next dashboard
  start. These are Chromium tests with simulated native bridges, not native
  Android WebView or Cloudflare tests. Private lab certificate validation is
  bypassed only in Chromium; the proxy still enforces the client certificate.
- No Proxmox host is configured in the frontend lab; the saved VM card and
  picker previews render the normal choose-device state. Live operations,
  native Android WebView 153, and the owner's Cloudflare endpoint remain
  outside the successful lab coverage.
- Release metadata target: `2026.9.1.20`; helper v5 and protocol v1 unchanged.
- **OWNER VALIDATION — PASS** on HA Core 2026.9.4, Companion 2026.8.4,
  `hapka.hubinet.pl`, LTE/mTLS with VPN off and no diagnostic force-load.
  Cold start, repeated app starts, resume from background, Wi-Fi/LTE changes
  in both directions, HA restart/reconnect and app restart afterward all pass.
  Both local/external desktop access, the card picker, and pre-existing saved
  cards pass. Exactly one native `module` resource was added automatically:
  `/hubinet_ops_static/hubinet-ops-cards.js?v=2026.9.1.20`; no duplicate exists.
  The .19 failure did not recur. This establishes the real owner-case fix,
  beyond the lab's earlier simulated-native checks.
- P3 cleanup/uninstall hygiene: **REAL / SUPPORTED CONFIG-ENTRY FIX ACCEPTED
  BY OWNER / MERGED IN PR #25**, within release line .20.
  Pre-implementation documentation checkpoint: `b1f0a42`.
  HA's `async_remove_entry` runs after deletion and supports last-host cleanup
  through the native resource collection, deleting only the exact owned
  relative path. Unload/reload and removal while another host remains preserve
  the resource; entry setup restores it when a host is added again in the same
  HA runtime, without registering the static route twice. YAML stays operator
  configured. HACS 2.0.5 removes files without deleting the custom integration's
  config entries; full cleanup cannot be promised when files disappear first.
  README documents removing all host entries in HA before HACS uninstall, and
  manual removal of an exact leftover resource through HA's Resources UI.
  P3 retained the .20 release pins; no .21 runtime version was introduced.
- Review readiness: **MERGED IN PR #25 / TAGGED AS 2026.9.1.20**.
  The owner validation covers RC `2026.9.1.rc.01.20`; the subsequent P3 backend
  lifecycle fix is covered by the native HA config-entry/resource tests above.

## Next

Implement the accepted Checkpoints C and D on the existing A/B/C/D Draft PR.
The whole stage uses planned release .21. Do not merge, tag, or create a
release. Tag `2026.9.1.16` (main `0409d76`) if still wanted.

## Explicitly not started

- Tag of `2026.9.1.16`.
- Pause and Resume in the VM card, and reading `qmpstatus`.
