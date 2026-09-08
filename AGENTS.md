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
architecture accepted. Until that decision is reflected in merged
`ARCHITECTURE.md`, the proposal remains unaccepted. Implementation in a draft
pull request does not make its design accepted.

An agent may propose architecture. An agent may not accept architecture on
behalf of the maintainer.

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

Before handing work off, verify that the implementation, tests, and docs agree
with the accepted architecture and current status. State any unresolved scope,
infrastructure, or architecture issue plainly.
