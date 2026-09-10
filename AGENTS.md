# Repository orientation

Hubinet-Ops is a Home Assistant custom integration built as a domain-isolated
fork of the official Home Assistant Proxmox VE integration. This repository is
an early rewrite. Its accepted architecture is intentionally small.

Before coding or reviewing, read in this order:

1. This file, `AGENTS.md`.
2. [PRODUCT.md](PRODUCT.md) for what the product is and why it exists.
3. [ARCHITECTURE.md](ARCHITECTURE.md) for the accepted architecture today.
4. [STATUS.md](STATUS.md) for what is merged, under review, and next.
5. [UPSTREAM.md](UPSTREAM.md) if touching upstream-derived code.
6. [DEVELOPMENT.md](DEVELOPMENT.md) if development or tests are involved.
7. Only then inspect implementation code.

Agents must understand the project state before coding. `main` plus merged
documentation define the accepted architecture. Draft pull-request code is not
architectural truth, and review findings are not architectural decisions until
they are accepted.

## Repository state before editing

Before editing or implementing, use the equivalent of `git status --short`,
`git branch --show-current`, and `git fetch origin` to establish:

- the worktree state;
- the current and expected task branches; and
- whether the relevant remote branch has moved or diverged.

If the worktree contains unrelated changes, or branch/history does not match
the assigned task, stop and report instead of guessing. Do not automatically
stash, use `reset --hard`, force-push, silently discard local work, or otherwise
rewrite state to continue.

## Sources and design authority

- The official Home Assistant `proxmoxve` integration and the Proxmox VE API
  are the preferred solutions wherever they already solve the problem.
- Every new custom layer must justify why upstream or the PVE API cannot solve
  the concrete need.
- The old `hubinet-ops` repository is a donor of proven behavior only. It is
  not a source of architectural or product truth.
- Never port legacy architecture unless it has been explicitly approved for
  this repository.
- Do not design features "for the future." Add architecture only for a
  demonstrated, accepted need.
- Do not expand task scope.
- Do not silently rewrite project architecture while implementing a feature.
- Review findings identify concerns; they do not establish a design until the
  design is explicitly accepted and documented.

The coding agent is not allowed to become the architect by accident.

### Architecture acceptance

Coding and review agents may identify problems, propose designs, compare
alternatives, and recommend architecture. Architectural acceptance is a
maintainer/human decision: agents must not unilaterally declare a new
architecture accepted.

Acceptance is established only by an explicit maintainer decision, recorded
either:

- in already-merged `ARCHITECTURE.md`; or
- by an explicit maintainer instruction governing the current task, in which
  case the same pull request records that decision in `ARCHITECTURE.md` and
  marks it accepted in `STATUS.md`.

A separate documentation-only pull request is therefore not required when the
maintainer has already approved the design for the work in hand. Implementing
more than the maintainer approved, or a different design from the one
approved, is not covered by that acceptance.

An agent may never treat acceptance as established because it authored or
modified `ARCHITECTURE.md` itself, because a design appears in a pull request,
or because a review recommended it. An agent may propose architecture. An
agent may not accept architecture on behalf of the maintainer.

#### Pre-implementation acceptance checkpoint

When architecture is proposed during the current task, the coding agent must
follow this sequence:

1. The maintainer explicitly accepts the design.
2. Record the accepted design and decision in `ARCHITECTURE.md`.
3. Mark the architecture accepted in `STATUS.md` and mark implementation as
   either `NOT STARTED` or `IN PROGRESS`.
4. Create a documentation checkpoint commit.
5. Begin runtime implementation only after that checkpoint.
6. After implementation, update the documentation again to describe the final
   post-merge state.

The checkpoint may live on the same feature branch and in the same pull
request; a separate documentation-only pull request is not required. Project
memory synchronization must not be deferred until handoff. The checkpoint
records the maintainer's decision; it does not allow an agent to accept
architecture, and review findings or recommendations remain insufficient for
acceptance. Implementation must stay within the exact scope the maintainer
approved.

## Prohibited architecture

Do not reintroduce any of the following without explicit architecture
approval:

- custom Proxmox inventory authority;
- duplicate PVE discovery;
- SQLite authority or store;
- custom backend;
- HTTP transport or backend;
- `hostd`;
- workers;
- schedulers;
- reconciliation;
- publication layer;
- resource UUID machinery;
- durable workflow or state machine;
- custom snapshot framework;
- custom rollback framework.

Absence from this list is not approval. New custom architecture still requires
a concrete need and consistency with [PRODUCT.md](PRODUCT.md) and
[ARCHITECTURE.md](ARCHITECTURE.md).

## Working rules

- Never implement directly on `main`; use a branch for feature work.
- Do not merge automatically.
- Do not rewrite history unless explicitly requested.
- Keep commits coherent and limited to the requested work.
- Report scope violations; do not hide them in a larger change.
- When unrelated infrastructure or test failures appear, report them instead
  of repairing unrelated systems.
- Documentation changes that alter architecture must be reviewed as
  architecture changes.
- Preserve the upstream relationship described in
  [UPSTREAM.md](UPSTREAM.md) when modifying upstream-derived code.
- Follow the repository-local environment and test workflow in
  [DEVELOPMENT.md](DEVELOPMENT.md); do not invent a parallel setup.

## Project memory in the definition of done

Documentation must describe the state that will exist after the pull request
is merged. In the same pull request:

- update `ARCHITECTURE.md` when accepted product or runtime architecture
  changes;
- update `STATUS.md` when merged, open, or next project state materially
  changes;
- update `PRODUCT.md` only when product intent or hard product rules change;
  and
- update `UPSTREAM.md` only when upstream provenance or baseline adaptations
  change.

Ordinary implementation that does not change architecture does not require an
architecture-document edit.

Architecture-bearing work therefore has two required synchronization points:
the pre-implementation acceptance checkpoint and the final pre-handoff
consistency check. The final check does not replace the earlier checkpoint.

Before handing work off, verify that the implementation, tests, and docs agree
with the accepted architecture and current status. State any unresolved scope,
infrastructure, or architecture issue plainly.
