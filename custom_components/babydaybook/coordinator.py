"""Keeps a small, fresh copy of each baby's recent activity."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SIDE_LEFT,
    SIDE_RIGHT,
    TYPE_BREASTFEEDING,
    TYPE_SLEEP,
    Baby,
    BabyDaybookApiError,
    BabyDaybookAuthError,
    BabyDaybookClient,
    BabyDaybookError,
    last_action,
    running_action,
    svt_sort_key,
)
from .const import (
    DOMAIN,
    FULL_REFRESH_INTERVAL,
    MAX_CACHED_ACTIONS,
    POLL_INTERVAL,
    RECENT_ACTIONS,
)

_LOGGER = logging.getLogger(__name__)

type BabyDaybookConfigEntry = ConfigEntry[BabyDaybookCoordinator]


@dataclass
class BabyState:
    """Recent activity of one baby, keyed by record id."""

    baby: Baby
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    cursor: dict[str, Any] | None = None

    def running(self, action_type: str) -> dict[str, Any] | None:
        """Return the in-progress record of a type, if any."""
        return running_action(self.actions.values(), action_type)

    def last(self, action_type: str) -> dict[str, Any] | None:
        """Return the newest record of a type, if any."""
        return last_action(self.actions.values(), action_type)

    def merge(self, records: list[dict[str, Any]]) -> None:
        """Add or replace records and advance the sync cursor."""
        for record in records:
            self.actions[record["uid"]] = record
            raw = record.get("_svt")
            if raw and svt_sort_key(raw) > svt_sort_key(self.cursor):
                self.cursor = raw
        if len(self.actions) > MAX_CACHED_ACTIONS:
            newest = sorted(
                self.actions.values(),
                key=lambda action: action.get("startMillis", 0),
                reverse=True,
            )[:MAX_CACHED_ACTIONS]
            self.actions = {action["uid"]: action for action in newest}


class BabyDaybookCoordinator(DataUpdateCoordinator[dict[str, BabyState]]):
    """Polls Baby Daybook and performs writes for the entities."""

    config_entry: BabyDaybookConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BabyDaybookConfigEntry,
        client: BabyDaybookClient,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        self.client = client
        self._states: dict[str, BabyState] = {}
        self._last_full = 0.0
        self._delta_supported = True

    async def _async_update_data(self) -> dict[str, BabyState]:
        try:
            if not self._states:
                babies = await self.client.list_babies()
                self._states = {baby.uid: BabyState(baby) for baby in babies}
            full = time.monotonic() - self._last_full > FULL_REFRESH_INTERVAL.total_seconds()
            for state in self._states.values():
                await self._refresh(state, full=full)
            if full:
                self._last_full = time.monotonic()
        except BabyDaybookAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BabyDaybookError as err:
            raise UpdateFailed(str(err)) from err
        return dict(self._states)

    async def _refresh(self, state: BabyState, *, full: bool = False) -> None:
        """Bring one baby up to date, cheaply when possible."""
        if not full and self._delta_supported and state.cursor:
            try:
                state.merge(await self.client.changed_actions(state.baby.uid, state.cursor))
            except BabyDaybookApiError as err:
                _LOGGER.warning(
                    "Incremental sync is not available (%s); reloading recent "
                    "activity on every poll instead",
                    err,
                )
                self._delta_supported = False
            else:
                return
        records = await self.client.recent_actions(state.baby.uid, RECENT_ACTIONS)
        state.actions = {}
        state.merge(records)

    # ----- actions used by the entities ------------------------------------

    async def _fresh_state(self, baby_uid: str) -> BabyState:
        """Return a baby's state after checking for changes made on the phones."""
        state = self._states[baby_uid]
        await self._refresh(state)
        return state

    def _publish(self, state: BabyState, record: dict[str, Any]) -> None:
        state.actions[record["uid"]] = record
        self.async_set_updated_data(dict(self._states))

    async def async_log_diaper(self, baby_uid: str, *, pee: bool, poo: bool) -> None:
        """Record a diaper change."""
        try:
            record = await self.client.log_diaper(baby_uid, pee=pee, poo=poo)
        except BabyDaybookError as err:
            raise HomeAssistantError(f"Could not log the diaper change: {err}") from err
        self._publish(self._states[baby_uid], record)

    async def async_set_sleep(self, baby_uid: str, asleep: bool) -> None:
        """Start or stop the sleep timer; does nothing if already in that state."""
        try:
            state = await self._fresh_state(baby_uid)
            running = state.running(TYPE_SLEEP)
            if asleep and not running:
                record = await self.client.start_timer(baby_uid, TYPE_SLEEP)
            elif not asleep and running:
                record = await self.client.stop_timer(running)
            else:
                self.async_set_updated_data(dict(self._states))
                return
        except BabyDaybookError as err:
            raise HomeAssistantError(f"Could not update sleep: {err}") from err
        self._publish(state, record)

    async def async_set_breastfeeding(self, baby_uid: str, side: str | None) -> None:
        """Feed on a side (starting or switching as needed), or stop with None."""
        if side not in (SIDE_LEFT, SIDE_RIGHT, None):
            raise HomeAssistantError(f"Unknown breastfeeding side: {side}")
        try:
            state = await self._fresh_state(baby_uid)
            running = state.running(TYPE_BREASTFEEDING)
            if side is None and running:
                record = await self.client.stop_timer(running)
            elif side and not running:
                record = await self.client.start_timer(
                    baby_uid, TYPE_BREASTFEEDING, side=side
                )
            elif side and running and running.get("side") != side:
                record = await self.client.switch_side(running, side)
            else:
                self.async_set_updated_data(dict(self._states))
                return
        except BabyDaybookError as err:
            raise HomeAssistantError(f"Could not update breastfeeding: {err}") from err
        self._publish(state, record)
