# Product

Hubinet-Ops is a Home Assistant custom integration that extends the official
Home Assistant Proxmox VE integration.

Its purpose is to add a small set of operator-controlled guest package
management capabilities that Home Assistant and the Proxmox VE API do not
natively provide. The official upstream integration remains responsible for
native Proxmox functionality.

## Upstream product boundary

Upstream owns:

- configuration flow;
- authentication and token handling;
- `proxmoxer`;
- PVE API access;
- node discovery;
- VM discovery;
- LXC discovery;
- the coordinator;
- native entities and status;
- start, stop, and restart;
- native snapshot operations; and
- other native PVE functionality already available upstream.

Hubinet-Ops must not maintain a second Proxmox inventory, a second discovery
system, or duplicate upstream state without a proven need. A native PVE
operation belongs through upstream or the PVE API. Custom host execution is
appropriate only where upstream and the PVE API genuinely cannot provide the
required guest operation.

## Intended custom scope

The current intended product scope is:

1. pending package scan for supported LXC guests;
2. package review;
3. explicit LXC package update;
4. explicit operator-controlled cleanup of current APT autoremove
   candidates; and
5. post-update health.

These are product intentions, not claims that the features or their
architecture are accepted or implemented. See [STATUS.md](STATUS.md) for the
current state and [ARCHITECTURE.md](ARCHITECTURE.md) for accepted design.

## Operator-control rules

- Package updates are never automatic.
- An update requires explicit operator action.
- Package cleanup is never automatic and requires explicit operator action.
- A non-empty cleanup plan must be shown to the operator and re-verified before
  cleanup mutation.
- The operator must be able to see and review the plan before an update.
- The executed package plan must match the reviewed plan.
- If the plan changes, the previous review must not authorize the new plan.
- A failed scan is `UNKNOWN`, never equivalent to zero available updates.
- An unsupported or unavailable guest is not equivalent to zero available
  updates.

The product should use the smallest correct architecture. New architecture
exists only to solve a demonstrated problem; anticipated future complexity is
not sufficient justification.

## Practical trust and safety model

The product trusts:

- the self-administered Proxmox environment;
- the PVE host administrator or root user;
- the Home Assistant operator; and
- normal Debian and Ubuntu `apt`/`dpkg` behavior.

Within that trust model, ordinary engineering safety still applies:

- use least privilege;
- do not accept arbitrary remote shell input;
- expose fixed, typed operations;
- validate targets;
- bound execution;
- handle credentials safely; and
- fail closed when the target or result cannot be established.

Hubinet-Ops does not adopt the legacy repository's hostile-administrator or
formal-attestation architecture.
