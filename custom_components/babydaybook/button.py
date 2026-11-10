"""Buttons that record a diaper change."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Baby
from .coordinator import BabyDaybookConfigEntry, BabyDaybookCoordinator
from .entity import BabyDaybookEntity

# key -> (pee, poo)
DIAPERS: dict[str, tuple[bool, bool]] = {
    "diaper_wet": (True, False),
    "diaper_dirty": (False, True),
    "diaper_mixed": (True, True),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyDaybookConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the diaper buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        DiaperButton(coordinator, state.baby, key)
        for state in coordinator.data.values()
        for key in DIAPERS
    )


class DiaperButton(BabyDaybookEntity, ButtonEntity):
    """Records one kind of diaper change when pressed."""

    def __init__(self, coordinator: BabyDaybookCoordinator, baby: Baby, key: str) -> None:
        """Initialise the button."""
        super().__init__(coordinator, baby, key)
        self._pee, self._poo = DIAPERS[key]

    async def async_press(self) -> None:
        """Record the diaper change."""
        await self.coordinator.async_log_diaper(
            self._baby_uid, pee=self._pee, poo=self._poo
        )
