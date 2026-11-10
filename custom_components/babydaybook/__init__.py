"""Baby Daybook integration: log diapers, sleep and feeding from Home Assistant."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BabyDaybookClient
from .const import CONF_REFRESH_TOKEN
from .coordinator import BabyDaybookConfigEntry, BabyDaybookCoordinator

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: BabyDaybookConfigEntry) -> bool:
    """Set up Baby Daybook from a config entry."""

    def _store_refresh_token(token: str) -> None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    client = BabyDaybookClient(
        async_get_clientsession(hass),
        entry.data[CONF_REFRESH_TOKEN],
        on_refresh_token=_store_refresh_token,
    )
    coordinator = BabyDaybookCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BabyDaybookConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
