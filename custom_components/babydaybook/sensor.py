"""Sensors showing when things last happened."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import TYPE_BREASTFEEDING, TYPE_DIAPER, TYPE_SLEEP, Baby
from .coordinator import BabyDaybookConfigEntry, BabyDaybookCoordinator
from .entity import BabyDaybookEntity

# key -> activity type
SENSORS = {
    "last_diaper": TYPE_DIAPER,
    "last_sleep": TYPE_SLEEP,
    "last_breastfeeding": TYPE_BREASTFEEDING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BabyDaybookConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        LastActivitySensor(coordinator, state.baby, key)
        for state in coordinator.data.values()
        for key in SENSORS
    )


class LastActivitySensor(BabyDaybookEntity, SensorEntity):
    """The start time of the newest activity of one type."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: BabyDaybookCoordinator, baby: Baby, key: str) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, baby, key)
        self._type = SENSORS[key]

    @property
    def native_value(self) -> datetime | None:
        """Return when the newest activity started."""
        action = self.baby_state.last(self._type)
        if not action:
            return None
        return datetime.fromtimestamp(action["startMillis"] / 1000, UTC)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return details of the newest activity."""
        action = self.baby_state.last(self._type)
        if not action:
            return None
        attributes: dict[str, Any] = {"in_progress": bool(action.get("inProgress"))}
        if self._type == TYPE_DIAPER:
            attributes["wet"] = bool(action.get("pee"))
            attributes["dirty"] = bool(action.get("poo"))
        else:
            if end := action.get("endMillis"):
                attributes["ended_at"] = datetime.fromtimestamp(end / 1000, UTC)
            if self._type == TYPE_BREASTFEEDING:
                attributes["side"] = action.get("side") or None
        return attributes
