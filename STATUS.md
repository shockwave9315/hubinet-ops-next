# Status

Status date: 2026-09-08

## Current baseline

- Upstream: Home Assistant Core tag `2026.9.1`, commit
  `fc034572d0216a04ed40a07154394908a594dfed`.
- Baseline tag: `baseline-ha-2026.9.1`.
- Accepted runtime architecture: a domain-isolated custom-integration fork of
  Home Assistant Core `proxmoxve`, with only the adaptations documented in
  [UPSTREAM.md](UPSTREAM.md).

## Merged

- Clean upstream-derived Proxmox VE integration baseline.
- Reproducible local development and Home Assistant test environment from
  branch `chore/dev-environment`.

## Open work

PR #2, branch `feat/package-scan`:

- is **DRAFT**;
- is undergoing architecture review;
- must not be merged yet; and
- must not be used as the architecture for package review, update, or health.

An independent read-only architecture review is in progress. Review findings
are not accepted fixes or architectural decisions yet.

### Known architecture questions for PR #2

- SSH or network endpoint versus PVE node identity.
- Failed scans must not leave stale data presented as current.
- Long-running package scans must not interfere with native Proxmox actions.
- Target freshness and VMID/node race handling.
- Correct minimal state ownership.
- A viable future scan/review/update/health extension path.

## Next

1. Finish the PR #2 architecture review.
2. Decide the accepted package-scan architecture.
3. Update [ARCHITECTURE.md](ARCHITECTURE.md).
4. Implement only accepted PR #2 corrections.
5. Review and merge package scan.
6. Only then design package review.

## Explicitly not started

- Package review.
- Package update.
- Post-update health.

Do not start those future stages before the package-scan architecture is
accepted.
