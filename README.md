# Hubinet-Ops

Hubinet-Ops is a Home Assistant custom integration for Proxmox VE. This first
baseline is a domain-isolated fork of Home Assistant Core's `proxmoxve`
integration from tag `2026.9.1`. The project is in an early rewrite and baseline
stage; custom package-management architecture is not yet accepted.

Copy `custom_components/hubinet_ops` into the `custom_components` directory of
a Home Assistant `2026.9.1` installation, restart Home Assistant, and add
**Hubinet-Ops** from **Settings > Devices & services**.

See [PRODUCT.md](PRODUCT.md) for product scope,
[DEVELOPMENT.md](DEVELOPMENT.md) for the local development workflow, and
[UPSTREAM.md](UPSTREAM.md) for the exact source commit and adaptation scope.
