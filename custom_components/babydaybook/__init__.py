"""Baby Daybook integration: log diapers, sleep and feeding from Home Assistant."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .actions import async_setup_actions
from .api import BabyDaybookClient
from .const import CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import BabyDaybookConfigEntry, BabyDaybookCoordinator

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services and voice intents once."""
    async_setup_actions(hass)
    return True


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
