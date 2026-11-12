"""Keeps a small, fresh copy of each baby's recent activity."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
    MAX_END_CORRECTION,
    OUTCOME_CORRECTED,
    OUTCOME_DELETED,
    OUTCOME_LOGGED,
    OUTCOME_STARTED,
    OUTCOME_STOPPED,
    OUTCOME_SWITCHED,
    OUTCOME_UNCHANGED,
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

    # ----- actions used by the entities, services and voice intents ----------

    @property
    def babies(self) -> list[Baby]:
        """Return the babies of this account."""
        return [state.baby for state in self._states.values()]

    async def _fresh_state(self, baby_uid: str) -> BabyState:
        """Return a baby's state after checking for changes made on the phones."""
        state = self._states[baby_uid]
        await self._refresh(state)
        return state

    def _publish(self, state: BabyState, record: dict[str, Any] | None) -> None:
        if record:
            state.actions[record["uid"]] = record
        self.async_set_updated_data(dict(self._states))

    async def async_log_diaper(
        self, baby_uid: str, *, pee: bool, poo: bool, at: datetime | None = None
    ) -> str:
        """Record a diaper change, now or at a past moment."""
        at_millis = _past_millis(at)
        try:
            record = await self.client.log_diaper(
                baby_uid, pee=pee, poo=poo, at_millis=at_millis
            )
        except BabyDaybookError as err:
            raise HomeAssistantError(f"Could not log the diaper change: {err}") from err
        self._publish(self._states[baby_uid], record)
        return OUTCOME_LOGGED

    async def async_set_sleep(
        self, baby_uid: str, asleep: bool, at: datetime | None = None
    ) -> str:
        """State that the baby fell asleep or woke up, now or at a past moment.

        Without a time this starts or stops the timer and does nothing if it
        is already in that state. With a time it also corrects: "fell asleep
        at" moves the start of a running sleep, and "woke up at" moves the end
        of a sleep that was already stopped.
        """
        return await self._set_timer(baby_uid, TYPE_SLEEP, asleep, at, side="")

    async def async_set_breastfeeding(
        self, baby_uid: str, side: str | None, at: datetime | None = None
    ) -> str:
        """Feed on a side (starting or switching as needed), or stop with None."""
        if side not in (SIDE_LEFT, SIDE_RIGHT, None):
            raise ServiceValidationError(f"Unknown breastfeeding side: {side}")
        return await self._set_timer(
            baby_uid, TYPE_BREASTFEEDING, side is not None, at, side=side or ""
        )

    async def _set_timer(
        self,
        baby_uid: str,
        action_type: str,
        on: bool,
        at: datetime | None,
        *,
        side: str,
    ) -> str:
        at_millis = _past_millis(at)
        client = self.client
        record: dict[str, Any] | None = None
        outcome = OUTCOME_UNCHANGED
        try:
            state = await self._fresh_state(baby_uid)
            running = state.running(action_type)
            if on and not running:
                record = await client.start_timer(
                    baby_uid, action_type, side=side, start_millis=at_millis
                )
                outcome = OUTCOME_STARTED
            elif on and running and side and running.get("side") != side:
                _require_after(at_millis, running["startMillis"], "started")
                record = await client.switch_side(running, side, at_millis)
                outcome = OUTCOME_SWITCHED
            elif on and running and at_millis is not None:
                record = await client.retime(running, start_millis=at_millis)
                outcome = OUTCOME_CORRECTED
            elif not on and running:
                _require_after(at_millis, running["startMillis"], "started")
                record = await client.stop_timer(running, at_millis)
                outcome = OUTCOME_STOPPED
            elif not on and at_millis is not None:
                last = state.last(action_type)
                if not last or not last.get("endMillis"):
                    raise ServiceValidationError("There is nothing to correct yet")
                if abs(at_millis - last["endMillis"]) > MAX_END_CORRECTION.total_seconds() * 1000:
                    raise ServiceValidationError(
                        "No timer is running, and the last one ended at "
                        f"{_local_time(last['endMillis'])}, too far from "
                        f"{_local_time(at_millis)} to be a correction"
                    )
                _require_after(at_millis, last["startMillis"], "started")
                record = await client.retime(last, end_millis=at_millis)
                outcome = OUTCOME_CORRECTED
        except BabyDaybookError as err:
            raise HomeAssistantError(f"Baby Daybook update failed: {err}") from err
        self._publish(state, record)
        return outcome

    async def async_delete_last(self, baby_uid: str, action_type: str) -> str:
        """Delete the newest record of a type, to undo a mistake."""
        try:
            state = await self._fresh_state(baby_uid)
            last = state.last(action_type)
            if not last:
                raise ServiceValidationError("There is nothing recent to delete")
            record = await self.client.delete_action(last)
        except BabyDaybookError as err:
            raise HomeAssistantError(f"Baby Daybook update failed: {err}") from err
        self._publish(state, record)
        return OUTCOME_DELETED


def _past_millis(at: datetime | None) -> int | None:
    """Convert a moment to epoch milliseconds, refusing the future."""
    if at is None:
        return None
    if at > dt_util.utcnow() + timedelta(minutes=1):
        raise ServiceValidationError("The time must not be in the future")
    return int(at.timestamp() * 1000)


def _require_after(at_millis: int | None, start_millis: int, what: str) -> None:
    if at_millis is not None and at_millis <= start_millis:
        raise ServiceValidationError(
            f"{_local_time(at_millis)} is before it {what} "
            f"({_local_time(start_millis)})"
        )


def _local_time(millis: int) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(millis / 1000)).strftime("%H:%M")
