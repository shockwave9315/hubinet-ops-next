# Development

The development and test environment is pinned to Home Assistant Core
`2026.9.1` at commit `fc034572d0216a04ed40a07154394908a594dfed`.

## Requirements

- Python 3.14.2 or newer (the default command is `python3.14`);
- Git; and
- `uv`.

Set `PYTHON_BIN` when a compatible interpreter has a different command name.

## Bootstrap

Create the repository-local environment with:

```bash
scripts/bootstrap-dev.sh
```

The script:

- creates `.venv` with the supported Python;
- creates `.dev/home-assistant-core` and checks out the pinned commit detached;
- links this repository's tests into the Home Assistant test tree;
- installs Home Assistant runtime and test requirements plus the editable Core
  project;
- installs the integration requirements selected by Home Assistant for
  `proxmoxve`, Supervisor, and MQTT fixtures; and
- compiles the translations needed by the upstream pytest harness.

Both `.venv/` and `.dev/` are ignored by Git. `.venv` is disposable. The
bootstrap script and pinned upstream commit, not a preserved local environment,
are the reproducible source.

## Daily workflow

After bootstrap, reuse `.venv` and `.dev/home-assistant-core`. Do not create
random alternate virtual environments because one import fails, and do not
manually install missing packages one by one as a normal workflow.

Run the integration lint and complete test suite with:

```bash
scripts/test.sh
```

The script runs Ruff against `custom_components/hubinet_ops` using Home
Assistant's pinned configuration, then runs the integration tests through the
pinned Home Assistant harness. Extra arguments are passed to pytest:

```bash
scripts/test.sh -k diagnostics
```

To run only the same Ruff check:

```bash
.venv/bin/ruff check --config .dev/home-assistant-core/pyproject.toml \
  custom_components/hubinet_ops
```

## Rebuild from scratch

After confirming neither local directory contains work that must be retained,
remove `.venv` and `.dev/home-assistant-core`, then rerun:

```bash
scripts/bootstrap-dev.sh
```

Agents must reuse the existing repository-local environment when it is valid.
If upstream or test infrastructure requires unrelated system repair, stop and
report the failure rather than changing unrelated systems.
