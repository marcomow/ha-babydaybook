"""Base entity for Baby Daybook."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Baby
from .const import DOMAIN
from .coordinator import BabyDaybookCoordinator, BabyState


class BabyDaybookEntity(CoordinatorEntity[BabyDaybookCoordinator]):
    """An entity that belongs to one baby."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BabyDaybookCoordinator, baby: Baby, key: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._baby_uid = baby.uid
        self._attr_translation_key = key
        self._attr_unique_id = f"{baby.uid}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, baby.uid)},
            name=baby.name,
            manufacturer="Baby Daybook",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def baby_state(self) -> BabyState:
        """Return the latest known state of this entity's baby."""
        return self.coordinator.data[self._baby_uid]
