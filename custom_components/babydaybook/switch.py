"""Switches for the sleep and breastfeeding timers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import SIDE_LEFT, SIDE_RIGHT, TYPE_BREASTFEEDING, TYPE_SLEEP, Baby
from .coordinator import BabyDaybookConfigEntry, BabyDaybookCoordinator
from .entity import BabyDaybookEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyDaybookConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the timer switches."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    for state in coordinator.data.values():
        entities.append(SleepSwitch(coordinator, state.baby))
        entities.append(BreastfeedingSwitch(coordinator, state.baby, SIDE_LEFT))
        entities.append(BreastfeedingSwitch(coordinator, state.baby, SIDE_RIGHT))
    async_add_entities(entities)


def _started_at(action: dict[str, Any] | None) -> dict[str, Any] | None:
    if not action:
        return None
    return {
        "started_at": datetime.fromtimestamp(action["startMillis"] / 1000, UTC)
    }


class SleepSwitch(BabyDaybookEntity, SwitchEntity):
    """On while the baby's sleep timer is running."""

    def __init__(self, coordinator: BabyDaybookCoordinator, baby: Baby) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, baby, "sleep")

    @property
    def is_on(self) -> bool:
        """Return whether the baby is asleep."""
        return self.baby_state.running(TYPE_SLEEP) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return when the current sleep started."""
        return _started_at(self.baby_state.running(TYPE_SLEEP))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the sleep timer."""
        await self.coordinator.async_set_sleep(self._baby_uid, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the sleep timer."""
        await self.coordinator.async_set_sleep(self._baby_uid, False)


class BreastfeedingSwitch(BabyDaybookEntity, SwitchEntity):
    """On while the baby is feeding on this side."""

    def __init__(
        self, coordinator: BabyDaybookCoordinator, baby: Baby, side: str
    ) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, baby, f"breastfeeding_{side}")
        self._side = side

    def _running(self) -> dict[str, Any] | None:
        action = self.baby_state.running(TYPE_BREASTFEEDING)
        return action if action and action.get("side") == self._side else None

    @property
    def is_on(self) -> bool:
        """Return whether the baby is feeding on this side."""
        return self._running() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return when the current feeding started."""
        return _started_at(self._running())

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start feeding on this side, or switch to it."""
        await self.coordinator.async_set_breastfeeding(self._baby_uid, self._side)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop feeding, if this side is the active one."""
        if self.is_on:
            await self.coordinator.async_set_breastfeeding(self._baby_uid, None)
