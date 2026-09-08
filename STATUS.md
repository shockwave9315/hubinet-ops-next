# Status

Status date: 2026-09-08

## Current baseline

- Upstream: Home Assistant Core tag `2026.9.1`, commit
  `fc034572d0216a04ed40a07154394908a594dfed`.
- Baseline tag: `baseline-ha-2026.9.1`.
- Merged runtime: a domain-isolated custom-integration fork of Home Assistant
  Core `proxmoxve`, with only the adaptations documented in
  [UPSTREAM.md](UPSTREAM.md).
- Package scan is not part of the merged runtime.

## Merged

- Clean upstream-derived Proxmox VE integration baseline.
- Reproducible local development and Home Assistant test environment.
- Project-memory foundation defining product, architecture, status,
  provenance, development, and agent responsibilities.

## Package-scan architecture decision

- Independent read-only architecture audit: **COMPLETE**.
- Package-scan architecture: **ACCEPTED BY MAINTAINER** and documented in
  [ARCHITECTURE.md](ARCHITECTURE.md).
- The accepted design contract is not yet implemented or merged runtime.

## Open work

PR #2, branch `feat/package-scan`:

- remains **DRAFT**;
- does not yet conform to every part of the accepted architecture; and
- must **NOT MERGE** until corrected and reviewed.

Major implementation work before PR #2 can merge:

- extract the package-manager boundary;
- replace system-SSH subprocess transport with `asyncssh`;
- separate the SSH endpoint from PVE node identity;
- correct scan state and unknown semantics;
- isolate scan concurrency from native PVE buttons;
- add a helper global deadline and host-side lock;
- restore tri-state evidence;
- preserve the not-upgraded count;
- improve semantic failure classifications;
- bound Home Assistant sensor attributes;
- make helper protocol/version compatibility explicit; and
- validate target freshness near execution.

## Next

1. Merge this architecture documentation decision.
2. Update or rebase PR #2 onto the accepted architecture.
3. Implement only the accepted package-scan corrections.
4. Run the focused and full Hubinet test suite.
5. Review PR #2.
6. Merge package scan only when implementation matches
   [ARCHITECTURE.md](ARCHITECTURE.md).
7. Only then design package review.

## Explicitly not started

- Package review.
- Package update.
- Post-update health.

Do not start those stages before package scan is corrected, reviewed, and
merged.
