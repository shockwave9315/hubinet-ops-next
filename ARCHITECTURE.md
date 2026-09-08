# Architecture

This document defines the accepted architecture, including the implemented
package-scan runtime. Product intent is defined in [PRODUCT.md](PRODUCT.md),
current work in [STATUS.md](STATUS.md), and fork provenance in
[UPSTREAM.md](UPSTREAM.md).

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
  [UPSTREAM.md](UPSTREAM.md); and
- the package-scan subsystem described below.

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

Current single-host scope has no separate `package_scan_ssh_host`, cluster
routing, node-address map, static VMID allowlist, or manually synchronized
inventory. A future need for separate API and SSH endpoints requires another
explicit architecture decision. Ordinary VMID and execution-time target
validation remain required.

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
It uses the dedicated `.ssh/hubinet_ops` private key and `.ssh/known_hosts`
under the Home Assistant configuration directory. Password, keyboard-
interactive, agent, PKCS#11, GSS, and host-based client authentication are
disabled, and local SSH configuration is not loaded.

The PVE host helper remains a root-owned forced-command boundary. It accepts no
caller-supplied remote shell command text. It must:

- accept only typed, validated operations; currently only `scan_packages`;
- validate VMID and expected operation;
- use fixed `pct exec` command shapes;
- perform no package mutation during scan;
- return structured, bounded evidence;
- bound stdout and stderr; and
- bound execution time.

The helper has one global operation deadline. Individual guest commands may
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

Future review may expose exact rows through an action/service response or
another explicit operator interaction, but that UI is not designed here.
Package-specific entities are exposed only when both required local SSH trust
files are present and non-empty. Adding or changing those files requires the
integration to be reloaded; no package-specific onboarding or probing system
exists.

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
