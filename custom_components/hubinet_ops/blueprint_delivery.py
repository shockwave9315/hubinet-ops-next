"""Provision only the integration's shipped, managed blueprint files."""

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.util.file import WriteError, write_utf8_file

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_SOURCE = Path(__file__).parent / "blueprints"
_FILES = (
    ("automation", "hubinet_ops_daily_package_scan.yaml"),
    ("script", "hubinet_ops_one_click_update.yaml"),
)


def _provision(config_dir: Path) -> set[str]:
    """Synchronize the two owned destinations, without enumerating other files."""
    changed: set[str] = set()
    for domain, filename in _FILES:
        destination = config_dir / "blueprints" / domain / DOMAIN / filename
        try:
            content = (_SOURCE / domain / filename).read_bytes()
            try:
                existing = destination.read_bytes()
            except FileNotFoundError:
                existing = None
            if content == existing:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            # HA's helper writes a sibling temporary file and atomically replaces
            # the destination; binary mode preserves the shipped bytes exactly.
            write_utf8_file(str(destination), content, mode="wb")
        except OSError, WriteError:
            _LOGGER.exception(
                "Could not provision Hubinet-Ops blueprint %s", destination
            )
        else:
            changed.add(domain)
    return changed


async def async_provision_blueprints(hass: HomeAssistant) -> None:
    """Make managed blueprints available without creating user instances."""
    changed = await hass.async_add_executor_job(_provision, Path(hass.config.path()))
    # An integration reload may replace a blueprint that HA already cached.
    for domain in changed:
        if blueprints := hass.data.get("blueprint", {}).get(domain):
            await blueprints.async_reset_cache()
