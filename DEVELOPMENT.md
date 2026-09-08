# Local development

The development and test environment is pinned to Home Assistant Core
`2026.9.1` at commit `fc034572d0216a04ed40a07154394908a594dfed`.

Bootstrap it with:

```bash
scripts/bootstrap-dev.sh
```

The script requires Python 3.14.2 or newer (by default `python3.14`), Git, and
`uv`. Set `PYTHON_BIN` to select another compatible interpreter. It creates the
ignored `.venv`, checks out the pinned HA Core source under the ignored
`.dev/home-assistant-core`, and installs HA's official runtime, test, constraint,
and editable-project dependencies. The integration-specific requirements are
selected by HA's official installer for `proxmoxve` and for the global test
harness's Supervisor and MQTT fixtures; unrelated integrations from
`requirements_all.txt` are not installed. Finally, it compiles the English
translations required by HA's upstream pytest harness inside the ignored Core
checkout, matching the official CI pre-test step.

Run the integration lint and complete test suite with:

```bash
scripts/test.sh
```

Extra arguments are passed to pytest, for example
`scripts/test.sh -k diagnostics`.
