# Upstream provenance

Hubinet-Ops is forked from the official Home Assistant Core Proxmox VE
integration.

- Repository: `https://github.com/home-assistant/core`
- Tag: `2026.9.1`
- Commit: `fc034572d0216a04ed40a07154394908a594dfed`
- Source path: `homeassistant/components/proxmoxve`
- Test source path: `tests/components/proxmoxve`
- License: Apache License 2.0

The baseline copies every file from the source integration and every upstream
test, fixture, and snapshot. The integration changes are limited to:

- the integration domain (`proxmoxve` to `hubinet_ops`);
- the displayed integration name (`Proxmox VE` to `Hubinet-Ops`);
- the custom integration version (`2026.9.1.0`); and
- domain-qualified translation references; and
- `translations/en.json`, generated from the adapted `strings.json` because
  custom integrations load runtime translations from `translations/`.

The adapted tests change only module paths, domain values, snapshot platform
values, and the fixture that enables loading a custom integration.
