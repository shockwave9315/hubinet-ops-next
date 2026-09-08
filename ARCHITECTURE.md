# Architecture

This document describes only the architecture accepted on current `main`.
Product intent is defined in [PRODUCT.md](PRODUCT.md), current work in
[STATUS.md](STATUS.md), and fork provenance in [UPSTREAM.md](UPSTREAM.md).

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

The upstream integration owns:

- configuration;
- authentication;
- Proxmox connectivity through `proxmoxer`;
- the coordinator;
- node, VM, and LXC identity;
- discovery;
- native entities; and
- native PVE operations.

These responsibilities remain upstream responsibilities. Hubinet-Ops does not
introduce a parallel inventory, discovery service, backend, or state authority.

### Current Hubinet-Ops runtime ownership

On merged `main`, Hubinet-Ops owns only:

- its domain-isolated fork identity; and
- the technically required custom-integration adaptations recorded in
  [UPSTREAM.md](UPSTREAM.md).

No package-management feature is part of the accepted runtime architecture.

## Architecture under review

PR #2, branch `feat/package-scan`, investigates this candidate flow:

```text
Home Assistant
    -> package scan trigger
    -> restricted host execution
    -> pct exec
    -> apt/dpkg
    -> package evidence
    -> HA sensor
```

**DRAFT — NOT MERGED — NOT ARCHITECTURAL TRUTH**

The candidate must not be used as a foundation for later features until its
architecture is reviewed, accepted, merged, and reflected in this document.

### Open architecture questions

The current read-only architecture audit must determine:

- the correct Home Assistant-to-PVE guest-execution boundary;
- how PVE node identity relates to an SSH or network endpoint;
- how target freshness and time-of-check/time-of-use races are handled;
- the semantics of stale, failed, and unknown scan results;
- how long-running scan concurrency interacts with native Proxmox actions;
- where the minimal package-scan state is owned; and
- how the design can later support
  `scan -> review -> verify -> update -> health`.

These are open questions, not accepted solutions. Do not resolve them by
assertion in documentation or by incidental implementation.

Any future accepted architecture must support those stages without requiring a
second Proxmox inventory, discovery system, or backend stack.
