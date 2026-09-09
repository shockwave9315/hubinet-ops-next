# Architecture

This document defines the accepted architecture, including the implemented
package-scan and package-review runtime. Product intent is defined in
[PRODUCT.md](PRODUCT.md), current work in [STATUS.md](STATUS.md), and fork
provenance in [UPSTREAM.md](UPSTREAM.md).

## Accepted architecture today

### Provenance

```text
homeassistant/core
proxmoxve @ 2026.9.1
        |
        | fork / copied baseline
        v
custom_components/hubinet_ops
```

Hubinet-Ops is a domain-isolated copy of the upstream integration, not a
runtime wrapper around or caller of `homeassistant.components.proxmoxve`.
Upstream behavior and structure are the baseline and should remain as close as
practical to the official `proxmoxve` integration.

### Runtime

```text
Home Assistant
    |
    v
custom_components/hubinet_ops
    |
    v
proxmoxer
    |
    v
Proxmox VE API
```

The installed Hubinet-Ops integration calls `proxmoxer` directly. It does not
delegate at runtime to an installed official `proxmoxve` integration.

### Upstream ownership

The upstream-derived integration continues to own:

- config flow;
- authentication;
- the Proxmox API connection through `proxmoxer`;
- coordinator refresh;
- node, VM, and LXC identity;
- discovery;
- native entities;
- native lifecycle operations; and
- native snapshots and other native PVE functionality.

Package code consumes upstream identity and state; it does not replace this
ownership. It must not introduce a second Proxmox inventory, duplicate
discovery, resource UUID authority, reconciliation, publication, SQLite
authority, a backend HTTP service, `hostd`, or a generic job framework.

### Hubinet-Ops runtime ownership

Hubinet-Ops owns:

- its domain-isolated fork identity;
- the technically required custom-integration adaptations recorded in
  [UPSTREAM.md](UPSTREAM.md);
- the guided-enrollment bootstrap described below; and
- the package-scan subsystem described below.

## Guided fresh-install enrollment

The default config-flow path is a guided setup layered beside the preserved
upstream-compatible existing-credentials path. It first collects only the
user-entered PVE API host, port, and SSL-verification preference. Duplicate
host detection happens at that point, before any bootstrap instruction is
shown.

The next form displays one command pinned to the exact installed integration
version. The root-run PVE bootstrap provisions only the fixed product
resources: user `hubinetnext@pve`, role `HubinetOpsNext`, token ID `ha`, the
root ACL, the forced-command helper, and the restricted key in
`/root/.ssh/authorized_keys2`. The exact role privileges are
`Datastore.Audit`, `Sys.Audit`, `Sys.PowerMgmt`, `VM.Audit`, `VM.PowerMgmt`,
and `VM.Snapshot`. The bootstrap verifies that effective sshd configuration
already enables `.ssh/authorized_keys2`; it never edits sshd configuration or
the other authorized-key files. For this release, `authorized_keys2` is an
exclusive Hubinet-owned file: foreign contents stop bootstrap before any
provisioning mutation and are never merged or rewritten. Bootstrap creates
`/root/.ssh` with root ownership and mode `0700` only when it is absent; it
does not change metadata on an existing directory. It enforces root ownership
and mode `0600` whenever it creates or replaces its own `authorized_keys2`
file.

The bootstrap prints one bounded `HUBINET1-` base64url JSON enrollment value.
It carries only the version, PVE token secret, compact Ed25519 private client
key, and local PVE Ed25519 SSH host public key. The value is sensitive,
temporary setup transport and is never persisted verbatim, but remains usable
while the credentials it contains remain valid. Operators discard terminal
and clipboard copies after enrollment.

The bootstrap has three finite lifecycle results. A fresh run reconciles the
fixed role, user, root ACL, and exact release helper, creates the two
credentials, and emits enrollment. A plain repair run always reconciles those
deterministic resources but never rotates valid credentials and emits no
enrollment when they already exist. If its fixed token exists but the Hubinet
key in `authorized_keys2` is missing or broken, deterministic repair may
complete, but bootstrap reports incomplete credential state and directs the
operator to `--reset` and Home Assistant re-enrollment. Explicit `--reset`
reconciles deterministic resources, rotates only the Hubinet SSH client key
and fixed API token, and emits new enrollment; those replacements invalidate
the old Hubinet credentials.

Before creating a config entry, Home Assistant strictly parses and imports the
enrollment, validates the fixed API identity against the user-entered API
endpoint, connects to the user-entered SSH endpoint using only the in-memory
private key and enrolled pinned host key, and invokes the helper's typed
`probe` operation. Protocol version is the compatibility authority; helper
version is informational. The authenticated probe returns the local PVE node,
which must be present in upstream API discovery. Only then is one entry created
with the token secret, private key, host public key, and `package_node` in
`entry.data`. There is no setup store, filesystem credential staging, custom
inventory, or durable enrollment state. For an existing guided entry,
Reconfigure offers guided re-enrollment beside the upstream-compatible
advanced credential path. Guided reauth uses the same `--reset` enrollment
pipeline. Endpoint, API auth, SSH trust, `package_node`, and discovered nodes
are replaced atomically and the entry is reloaded only after every check
succeeds. An advanced host change clears stale package SSH trust while leaving
native Proxmox functionality available.

Config-entry version 4 removes runtime use of `/config/.ssh/hubinet_ops` and
`/config/.ssh/known_hosts`. Migration imports an existing file-based identity
only when both files, the configured host key, and a single upstream node are
unambiguous; any active OpenSSH marker line refuses the whole import, and the
migration never deletes or modifies the old files. An unsafe or ambiguous
legacy identity is left unimported, so native Proxmox operation continues while
package controls remain unavailable until Reconfigure → Re-enroll. Malformed
package trust already stored in an entry likewise disables only package
controls and cannot prevent the upstream-derived native integration from
loading.

## Implemented package-scan architecture

This section describes the implemented form of the package-scan architecture
accepted by the maintainer.

### Ownership boundary

Package behavior belongs in a small subsystem with a package manager as its
boundary:

```text
Upstream HA ProxmoxVE-derived coordinator
        |
        | node / VMID / runtime state
        v
packages/manager.py
        |
        +---- ephemeral scan state
        +---- package-scan concurrency
        +---- package transport
        |
        v
async SSH transport
        |
        v
forced-command helper on PVE host
        |
        v
pct exec <vmid>
        |
        v
Debian/Ubuntu guest
        |
        +---- apt
        +---- dpkg
        |
        v
package evidence
        |
        v
pure package parser
        |
        v
ScanRecord
        |
        v
Home Assistant summary entities
```

The exact Python file layout may vary slightly, but these responsibility
boundaries are binding. The upstream-derived coordinator supplies identity and
runtime inputs and may hold a package-manager reference for integration
composition. It must not become the package subsystem or accumulate package
scan, review, update, health, or package-specific orchestration state and
methods.

### Identity and endpoint

Network endpoint and PVE resource identity are separate:

- SSH endpoint: the existing configured Proxmox `CONF_HOST`;
- expected PVE node: upstream coordinator node identity;
- VMID: upstream coordinator LXC identity; and
- current typed operation: `scan_packages`.

Each request is conceptually
`(endpoint, expected_node, vmid, operation)`. The SSH destination must not be
derived from the PVE node name. The helper response must carry enough target
identity to verify the expected node and VMID.

The helper-authenticated local node is stored as `package_node`. Package
sensors and buttons are created only for upstream-discovered LXCs whose node
equals that value. Native entities continue to cover every API-discovered
cluster node. Current single-host scope has no separate
`package_scan_ssh_host`, cluster routing, node-address map, static VMID
allowlist, or manually synchronized inventory. A future need for separate API
and SSH endpoints requires another explicit architecture decision. Ordinary
VMID and execution-time target validation remain required.

### State semantics

Package-scan state is ephemeral, package-subsystem-owned state, conceptually a
`ScanRecord`. It must distinguish never scanned, running, last attempt
succeeded, and last attempt failed.

| Latest scan state | Current pending-package sensor state |
| --- | --- |
| Success | Exact pending count, including zero |
| Never scanned | Unknown |
| Running | Unknown |
| Failed | Unknown |

A failed or running scan must not leave an earlier successful count presented
as current evidence. Bounded diagnostic metadata such as last-attempt time,
running status, and last error classification may be retained only when it
cannot be mistaken for current package evidence.

There is no persistent scan store. A Home Assistant restart may reset scan
state to never-scanned and unknown.

### Concurrency

Package scans have their own concurrency controls inside the package manager:

- at most one scan per VMID within a config entry; and
- at most two package scans concurrently per config entry (configured
  Proxmox host).

Each config entry (configured Proxmox host) owns one package manager and
therefore its own independent bounds. Two config entries for two different
Proxmox hosts do not throttle one another; this is not cluster routing, a
node-address map, a shared worker pool, or cross-entry coordination -- it
is ordinary per-entry state, consistent with the rest of this subsystem.

Locks or semaphores may implement these bounds. A scan trigger must not occupy
the native PVE button semaphore for the full remote scan, change native button
semantics, or change upstream `PARALLEL_UPDATES = 1`.

This design has no scheduler, durable queue, worker service, job database, or
retry worker.

### Host-control boundary

The accepted Home Assistant-side transport is `asyncssh`, not a local system
`ssh` subprocess:

```text
Home Assistant
    |
    v
asyncssh
    |
    v
PVE sshd
    |
    v
forced command
    |
    v
typed helper operation
```

Official Home Assistant integrations already use `asyncssh`. It fits native
asynchronous Home Assistant operation, avoids dependence on a system SSH
executable, and avoids client-side subprocess, process-group, and selector
machinery.

The integration connects to the configured `CONF_HOST` on SSH port 22 as root.
It uses only the Ed25519 private client key and the pinned Ed25519 PVE host key
stored in the config entry. Both are imported deliberately from in-memory
bytes; the user-entered host is combined with the enrolled public key to build
the trust input. The transport itself fails before connecting when either
trust input is absent, and host-key mismatch is distinct from authentication
failure. Password, keyboard-interactive, agent, PKCS#11, GSS, and host-based
client authentication are disabled, and local SSH configuration is not
loaded. Setup probe requests have a 30-second transport timeout; package scans
retain their 300-second transport timeout.

The PVE host helper remains a root-owned forced-command boundary. It accepts no
caller-supplied remote shell command text. It must:

- accept only typed, validated operations; currently only `scan_packages`;
- validate VMID and expected operation;
- use fixed `pct exec` command shapes;
- perform no package mutation during scan;
- return structured, bounded evidence;
- bound stdout and stderr; and
- bound execution time.

The helper accepts two operations: the read-only `probe`, which resolves only
the PVE-native local node and never calls `pct`, and `scan_packages`. The helper
has one global operation deadline. Individual guest commands may
have smaller bounds, but sequential commands must share the remaining global
time. The Home Assistant transport deadline must exceed the helper deadline so
the helper normally terminates with a classified result first. A compatible
shape is approximately 300 seconds for transport, 240 seconds globally in the
helper, and at most 120 seconds per command subject to remaining time; exact
constants are implementation details.

A simple, appropriately scoped host-side `flock` must prevent accidental
overlapping package/apt scans. It is ordinary concurrency protection, not
durable job ownership or a database-backed lock.

The helper validates target facts as close as practical to `pct exec` and
fails safely when the VMID is no longer an LXC, the guest is stopped or
unavailable when execution requires it, or expected node/target identity does
not match. Coordinator state must not be assumed fresh throughout a long scan.
No cryptographic incarnation proof, resource UUID authority, generation, or
fencing machinery is required by the trusted environment threat model in
[PRODUCT.md](PRODUCT.md).

Because the helper is separately deployed, responses expose a small explicit
compatibility contract: protocol version, helper or implementation version,
operation, and target identity. Incompatible protocol versions fail clearly
rather than appearing as arbitrary execution or parser failures. No generic
version-negotiation framework is needed.

### Parser and evidence rules

Parsing remains pure and separate from I/O. It preserves proven donor behavior
where appropriate:

- strict `/etc/os-release` handling and Debian/Ubuntu support;
- native architecture and exact `dpkg` inventory;
- binary identity as `(name, architecture)` with multiarch handling;
- APT `Inst` and `Conf` evidence;
- malformed-plan rejection;
- security-origin and reboot-required evidence;
- unfinished-`dpkg` detection; and
- bounded evidence.

Malformed or ambiguous evidence remains fail-closed.

Unfinished `dpkg` states—`half-installed`, `unpacked`, `half-configured`,
`triggers-awaited`, and `triggers-pending`—and APT evidence such as
`N not fully installed or removed` make the scan fail. They should receive a
specific semantic classification such as `DPKG_UNFINISHED`, not merely a
generic malformed-parser error. The helper must not run `dpkg --configure -a`
or otherwise auto-repair the guest.

When evidence is syntactically valid but contradicts because package state
changed between observations, use a semantic classification such as
`GUEST_CHANGED_DURING_SCAN` where distinguishable. No automatic retry is
required; the operator may scan again.

Evidence that is not reliably known remains tri-state:

- `reboot_required` is true with positive evidence, false only with reliable
  evidence that no reboot is required, and otherwise unknown;
- security classification is true with positive security-origin evidence,
  false with reliable non-security evidence, and otherwise unknown.

Missing or unreadable reboot evidence is not false. Missing or ambiguous
origin metadata is not non-security.

APT's not-upgraded/kept-back count is separate bounded evidence, such as
`not_upgraded_count`. It is not part of the pending, reviewable, or executable
package plan. For example, `3 upgraded, 41 not upgraded` means a pending plan
count of 3 and a not-upgraded count of 41.

### Home Assistant presentation

Package entities are bounded summary surfaces, not stores for full package
rows. Summary attributes may include OS information, reboot-required
tri-state, security and unknown-security counts, held-back/not-upgraded count,
last-attempt time, running status, and last error classification. Exact package
rows remain inside package-subsystem state.

Exact rows are exposed only through the package-review action described below.
Package-specific entities are exposed only when the config entry contains all
enrolled SSH trust and a `package_node`, and only for LXCs on that node.

### Future extension boundary

Clean responsibilities must allow this later path:

```text
scan
    -> review
    -> execution-time exact-plan verification
    -> explicit update
    -> post-update health
```

Future update execution must verify that the executed plan exactly matches the
reviewed plan. The only accepted preparation now is the boundary
`upstream identity/state -> package manager -> typed package operations -> pure
package evidence/parser`.

Review, update, and health are not implemented by this decision. It adds no
approval persistence, update jobs, snapshot orchestration, rollback, or health
machinery.

## Explicitly rejected or modified package-scan architecture

- **Rejected: stale successful count after failure.** Never-scanned, running,
  or latest-failed state makes the current package count unknown.
- **Rejected: relaxed unfinished-`dpkg` handling.** Unfinished package-manager
  state remains fail-closed and should be classified specifically.
- **Rejected: static VMID allowlists.** Upstream PVE identity and discovery
  remain the resource source of truth.
- **Modified: separate SSH endpoint option.** Current single-host scope reuses
  `CONF_HOST`; it adds no second endpoint option.
- **Accepted: forced-command SSH boundary.** The host helper remains a narrow,
  typed control boundary.
- **Accepted: `asyncssh` client transport.** Home Assistant uses native async
  SSH rather than the system `ssh` executable.

## Package review architecture

This section describes the implemented form of the package-review
architecture accepted by the maintainer and independently red-teamed
against Home Assistant Core `2026.9.1`; see [STATUS.md](STATUS.md) for
current implementation state. It builds on the package-scan architecture
above and does not replace any part of it.

### State ownership and lifetime

Review state belongs to the existing package subsystem (`PackageManager`) and
is ephemeral, exactly like scan state. There is no persistence, recovery, or
reconstruction of scan or review state after a Home Assistant restart,
integration reload, or `PackageManager` reconstruction. Uncertain ephemeral
state is discarded, not reconstructed; the operator scans and reviews again.
This design does not need to prove historical package state or historical LXC
identity.

Review adds no new object. The existing successful `PackageScanRecord`
conceptually gains two fields:

- `token: str | None`
- `reviewed: bool`

There is no separate review object, no duplicate copy of the reviewed plan, no
copy of the reviewed token, no review timestamp, no review database, and no
review history. `reviewed == True` means exactly that the `result.packages`
stored on this same immutable successful record were explicitly confirmed.
Confirmation preserves the existing `result` and scan token unchanged; the
record itself stays frozen, and every state transition replaces the whole
record rather than mutating it in place.

### Reviewed-plan equality

The exact reviewed/executable package plan is defined only by the canonically
ordered tuple of `(name, architecture, installed_version, candidate_version)`
taken from `result.packages`, using the parser's existing `(name,
architecture)` sort and duplicate-identity rejection. `origin`,
`security`, `os_id`, `os_version`, `reboot_required`, and
`not_upgraded_count` are informational only and never bear on equality.
`origin` and `security` remain visible to the operator as context. This
design does not expand the trust model into hostile-repository or
supply-chain identity.

### Scan token

Every successful package scan produces one fresh opaque scan token
(conceptually `secrets.token_urlsafe(16)` or equivalent ~128-bit JSON-safe
randomness), consistently called the **scan token**. The token:

- belongs to one successful scan observation and changes on every later
  successful scan, even when the package rows are unchanged;
- is present only for `SUCCESS`, and absent for `NEVER`, `RUNNING`, and
  `FAILED`;
- is discarded when the record is replaced, when the target is pruned, and on
  reload/restart;
- is never persisted or restored.

The token is not a secret, not authentication, not authority, not a resource
UUID, not resource identity, not a generation/incarnation identifier, not
attestation, and encodes no node, VMID, or config-entry data. Its only
purpose is optimistic concurrency between `get_package_plan` and
`confirm_package_review` (show scan A -> token A; new scan B -> token B;
confirming with token A now returns `reviewed: false` because the current
token is B). There is no monotonic revision counter, no seeded counter, and
no plan-content fingerprint used as the confirmation token.

### Invalidation is whole-record replacement

There is no separate invalidation subsystem; every transition replaces or
deletes the whole `PackageScanRecord`:

- **New scan starts:** the previous successful record is immediately replaced
  by `RUNNING`; the old review and old scan token are gone.
- **New scan succeeds:** a new `SUCCESS` record is created with a fresh scan
  token and `reviewed = False`, even when the rows are identical to the
  previous scan. Review is never carried forward.
- **Scan fails:** the record becomes `FAILED`; review is gone and no token is
  present.
- **Target disappears / is pruned:** the target's package record is deleted;
  review and token go with it.
- **VMID reappears later:** it starts with fresh package state and inherits
  no review. This design does not prove whether it is "the same LXC."
- **HA restart / integration reload:** all ephemeral state, including review
  and tokens, is gone; nothing is reconstructed.

### LXC stop/start does not invalidate review

Stopping an LXC does not invalidate its stored successful record: the
`PackageScanRecord`, its `reviewed` flag, and its scan token remain in RAM.
Review actions are simply unavailable while the package sensor is
unavailable (see below). When the LXC starts again, the stored result and
review become visible again with no automatic re-scan. This design adds no
historical-identity proof and no expiry based on elapsed time; future update
execution independently re-obtains a fresh exact package plan before any
mutation.

### No review TTL

There is no review expiry, no timer, no `reviewed_at` freshness deadline, and
no expiry callback. A timer cannot prove freshness: packages can change
before a deadline, and an unchanged plan can remain correct after one. The
freshness mechanism is future execution-time exact-plan equality (see
"Future update handoff"), not elapsed time.

### Empty plan

A successful scan with zero pending packages may still be viewed:
`get_package_plan` may return `status: success`, a token, `reviewed: false`,
and `packages: []`. Confirming an empty plan does not create review state; it
always returns `{"reviewed": false}`. There is no `reviewed = True` state for
a zero-package plan.

### Action surface

Exactly two package-review entity actions exist:

- `hubinet_ops.get_package_plan`
- `hubinet_ops.confirm_package_review`

Both are registered from the sensor platform and both use
`SupportsResponse.ONLY`. This is intentional even for confirmation, which
mutates ephemeral metadata: Home Assistant Core `2026.9.1` has no separate
"mutating action with mandatory response" mode, and the caller must receive
an explicit acknowledgement.

### Actions are restricted to `PackageScanSensor`

Registering an entity service from `sensor.py` does not automatically
restrict it to `PackageScanSensor`; the shared `(sensor, hubinet_ops)` entity
mapping also contains ordinary CPU, RAM, status, storage, and other
Hubinet-Ops sensors that must remain ineligible. `PackageScanSensor` must
therefore expose a private/custom supported-feature bit for package-review
actions, and both actions must be registered with native
`required_features`, using Home Assistant-native entity-service filtering.
This design adds no custom target resolver, no manual node/VMID routing, no
second inventory, and no custom config-entry lookup architecture.

### `get_package_plan` contract

The action returns one response per eligible package sensor and never
changes state; it is not review confirmation. For a successful scan it
returns `status`, `token`, `reviewed`, and `packages[]`, where each package
row is a primitive JSON dictionary containing at least `name`,
`architecture`, `installed_version`, `candidate_version`, `origin`, and
`security` (`origin`/`security` informational only, rows never truncated).
For `NEVER`, `RUNNING`, or `FAILED` it returns `status` only -- never a token
or exact rows.

### `confirm_package_review` contract

The input field is `token`; the target is selected through the package
sensor entity as usual. Confirmation succeeds only when the current record
exists, is successful, has a current token, that token equals the supplied
token, and the package plan is non-empty; on success the manager replaces
the record with `reviewed = True`, preserving the exact same `result` and
token, and returns `{"reviewed": true}`. Otherwise -- stale/wrong token, no
current successful scan, or an empty package plan -- it returns
`{"reviewed": false}`. These are expected business outcomes and must not
raise exceptions.

### Multi-target confirmation is independent per sensor

Home Assistant can dispatch one entity service call to multiple eligible
entities concurrently (for example confirming `sensor.ct106_...` and
`sensor.ct107_...` in one call with different `reviewed` results). There is
no cross-target transaction, no rollback, and no all-or-none review; a stale
token on one target must not raise an exception because another target
already succeeded. Expected business rejection is per-entity response data.
Unexpected programming/runtime failures may still raise normally.

### Unavailable targets

Package-review actions inherit `PackageScanSensor.available`; a stopped LXC
is unavailable. For a response-required action with no eligible target
remaining, Home Assistant Core provides the caller-visible failure -- no
special Hubinet-Ops unavailable-target routing layer is added. With mixed
targets, unavailable targets may be omitted while available eligible package
sensors return their own responses.

### Concurrency and atomicity

Both actions live on the sensor platform, where `PARALLEL_UPDATES = 0` holds
no entity-platform semaphore. Review actions must not use the native Proxmox
button semaphore, package-scan concurrency slots, a review queue, a
scheduler, or an `asyncio` lock. `confirm_package_review`'s check-and-write
(read current record, validate the current scan token, replace the record
with `reviewed = True`) must be one synchronous, event-loop-atomic operation
with no `await` between those steps, preserving the same stale
scan-completion object-identity protection used elsewhere in the subsystem.

### HA entity/state surfaces remain bounded

Exact package rows and the scan token are never placed in sensor attributes;
the token is obtainable only from `get_package_plan`, alongside the exact
rows it returns. The package sensor may expose only bounded review metadata,
`reviewed: bool`. There is no review entity, no review binary sensor, and no
review button -- a button is explicitly rejected because it cannot carry the
token previously returned with the viewed plan and would degrade to
"review whatever is current now," which is not acceptable.

### Exact rows are transient response data

Exact package rows returned by `get_package_plan` are primitive JSON
dictionaries and are transient action-response data only: they are not
sensor state, not sensor attributes, not recorder history, and not
persistent review storage. This design adds no custom WebSocket API, no
custom frontend, and no custom card or panel. The reviewable plan is never
truncated.

### Cross-target and restart token behavior

A token obtained from one package sensor (e.g. CT107) presented while
confirming a different package sensor (e.g. CT106) must return
`{"reviewed": false}`, because CT106's current successful record carries its
own, different fresh scan token; no target data needs to be encoded into the
token itself. After a reload/restart, a stale previously held token must not
confirm any new successful observation, because each new successful
observation receives a fresh random token. No resource-identity machinery is
required.

### Future update handoff

Package review does not design package execution. The future update feature
receives only the current package record when `reviewed` is `True`, plus
`result.packages`, projected as the canonically ordered tuple `(name,
architecture, installed_version, candidate_version)` per row. The future
feature must independently obtain a fresh exact package plan and require
`fresh_plan == reviewed_plan`; if they differ, it stops and requires the
operator to scan and review again. The scan token has no role in future
execution equality -- its job ends once review is confirmed. This decision
does not design `apt` update execution, ordering, retry, snapshot, rollback,
or post-update health.

### Inst/Conf symmetry and key lifecycle

Inst/Conf symmetry (`C1`) is deferred until update design: review rows and
equality fields are derived from `Inst`, `Conf` contributes no review
equality field, and the current parser already rejects a `Conf` that
configures something outside the `Inst` plan. This docs decision does not
modify package-parser architecture. Private-key lifecycle (`C2`) is closed
and is not an open package-review design concern.

### Explicitly rejected package-review architecture

Package review does not introduce: durable approval; persisted review;
review recovery; historical LXC identity proof; incarnation ID; resource
generation authority; resource UUID authority; fencing; reconciliation; a
review database; SQLite; backend HTTP; `hostd`; a worker; a scheduler; a
queue; a review TTL; a review timer; cryptographic plan attestation; token
authority; a content-derived confirmation hash; a duplicate reviewed-plan
copy; a generalized workflow/state machine; a custom frontend; a review
button; a review entity; a custom WebSocket API; a second inventory; a
custom target resolver; a cross-target transaction; package mutation;
package-update architecture; or post-update health architecture.
