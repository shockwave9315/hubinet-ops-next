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
   candidates;
5. operator-controlled native PVE snapshot selection and explicit Restore and
   Delete for QEMU VMs and LXCs; and
6. post-update health.

These are product intentions, not claims that the features or their
architecture are accepted or implemented. See [STATUS.md](STATUS.md) for the
current state and [ARCHITECTURE.md](ARCHITECTURE.md) for accepted design.

## Operator-control rules

- Package updates are never automatic.
- An update requires explicit operator action.
- The integration never initiates package cleanup by itself. Cleanup requires
  explicit operator action or the operator's opt-in post-Update Autoremove
  choice in the optional Easy UX script.
- A non-empty cleanup plan must be shown to the operator and re-verified before
  cleanup mutation.
- The operator must be able to see and review the plan before an update.
- The executed package plan must match the reviewed plan.
- If the plan changes, the previous review must not authorize the new plan.
- A failed scan is `UNKNOWN`, never equivalent to zero available updates.
- An unsupported or unavailable guest is not equivalent to zero available
  updates.
- A guest that leaves the running state invalidates its current package
  evidence; after it runs again, pending and unused package values remain
  `UNKNOWN` until a new Scan runs.
- Snapshot Restore is never automatic. It requires selection of one exact
  native PVE snapshot followed by a separate explicit Restore button press.
- Snapshot Delete is never automatic. It consumes an explicit exact choice
  from the same native selector; manually created and Hubinet-created snapshots
  are equally deletable. A naming warning never blocks deletion or adds a
  confirmation step.
- To preserve its own in-flight package reporting, Hubinet rejects deletion of
  an LXC `hubinet-preupd-*` snapshot while the same target's Update or Autoremove
  is RUNNING. This adds no ownership policy, confirmation, or persistent state;
  after the attempt ends, explicit deletion is allowed.
- Snapshot Delete does not roll back the guest or invalidate package/Health
  evidence. Native PVE locks and normal deletion semantics are authoritative.
- The native Proxmox VE API remains authoritative for snapshot operations.
- Once Hubinet starts submitting an accepted LXC Restore, current package
  truth is invalid: pending and unused values become `UNKNOWN`, and a manual
  Scan is required before package truth becomes actionable again.
- Package operations are refused for an LXC while its Hubinet Restore is
  running or its outcome remains uncertain in the current Home Assistant
  runtime.

The product should use the smallest correct architecture. New architecture
exists only to solve a demonstrated problem; anticipated future complexity is
not sufficient justification.

## Optional Easy Update UX

Easy Update UX composes Home Assistant entities and actions. The integration
ships and provisions its own blueprint templates; the user creates and enables
their automation and script instances. One automatic Scan automation uses the
thin Scan All action for every currently supported package LXC across loaded
entries, with no per-LXC selection. It keeps summary evidence useful;
an explicit Update click confirms the exact current plan token and presses the
existing Update button. Viewing individual package names is optional UX: the
count, security count, operation result, and Health summarize the decision,
while Review continues to expose the full exact plan on demand.

The user may choose Easy (scheduled Scan, one-click Update, no Autoremove), YOLO
(the same with opt-in Autoremove after successful Update and fresh candidates),
or Manual/Advanced (Scan -> Review -> Approve -> Update / Autoremove / Health).
These are optional presentation/orchestration profiles, not backend modes.
All profiles use the same unchanged backend and its exact-plan checks. A changed
plan requires new Scan evidence and another explicit click; the script never
silently scans and approves a replacement plan.

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
