"""Per-host daily automatic Scan, configured in the integration options."""

from datetime import datetime, time
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.event import async_track_time_change

from .const import CONF_VM_GUEST_MEMORY
from .services import async_scan_entry

_LOGGER = logging.getLogger(__name__)
CONF_AUTO_SCAN = "auto_scan"
CONF_AUTO_SCAN_TIME = "auto_scan_time"
DEFAULT_AUTO_SCAN_TIME = "06:00:00"


def _scan_time(entry: ConfigEntry) -> time:
    """Return the configured local Scan time, falling back to the default."""
    try:
        return time.fromisoformat(
            entry.options.get(CONF_AUTO_SCAN_TIME, DEFAULT_AUTO_SCAN_TIME)
        )
    except TypeError, ValueError:
        return time.fromisoformat(DEFAULT_AUTO_SCAN_TIME)


@callback
def async_setup_auto_scan(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register one daily local-time Scan for this entry when enabled."""
    if not entry.options.get(CONF_AUTO_SCAN, False):
        return
    at = _scan_time(entry)

    @callback
    def scan(_now: datetime) -> None:
        _LOGGER.debug("Automatic Scan for %s", entry.title)
        async_scan_entry(entry)

    entry.async_on_unload(
        async_track_time_change(
            hass, scan, hour=at.hour, minute=at.minute, second=at.second
        )
    )


class HubinetOpsOptionsFlow(OptionsFlowWithReload):
    """Per-host options: the daily automatic Scan and extended VM memory data."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the entry options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AUTO_SCAN, default=options.get(CONF_AUTO_SCAN, False)
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_AUTO_SCAN_TIME,
                        default=options.get(
                            CONF_AUTO_SCAN_TIME, DEFAULT_AUTO_SCAN_TIME
                        ),
                    ): selector.TimeSelector(),
                    vol.Required(
                        CONF_VM_GUEST_MEMORY,
                        default=options.get(CONF_VM_GUEST_MEMORY, False),
                    ): selector.BooleanSelector(),
                }
            ),
        )
