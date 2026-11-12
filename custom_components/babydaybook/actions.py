"""Services and voice intents: the same actions, exposed two ways.

Services are for automations and scripts. Intents are for voice: Home
Assistant offers every registered intent to AI conversation agents as a tool,
so "Robin woke up ten minutes ago" works without any per-user setup.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import intent
from homeassistant.util import dt as dt_util

from .api import (
    SIDE_LEFT,
    SIDE_RIGHT,
    TYPE_BOTTLE,
    TYPE_BREASTFEEDING,
    TYPE_DIAPER,
    TYPE_SLEEP,
)
from .const import (
    DOMAIN,
    MAX_BOTTLE_ML,
    OUTCOME_CORRECTED,
    OUTCOME_DELETED,
    OUTCOME_LOGGED,
    OUTCOME_STARTED,
    OUTCOME_STOPPED,
    OUTCOME_SWITCHED,
)
from .coordinator import BabyDaybookCoordinator

ATTR_BABY = "baby"
ATTR_KIND = "kind"
ATTR_SIDE = "side"
ATTR_ACTIVITY = "activity"
ATTR_AMOUNT = "amount"
ATTR_NOTE = "note"
ATTR_MINUTES_AGO = "minutes_ago"
ATTR_TIME = "time"

# kind -> (pee, poo)
DIAPER_KINDS: dict[str, tuple[bool, bool]] = {
    "wet": (True, False),
    "dirty": (False, True),
    "mixed": (True, True),
}
ACTIVITIES = {
    "diaper": TYPE_DIAPER,
    "sleep": TYPE_SLEEP,
    "breastfeeding": TYPE_BREASTFEEDING,
    "bottle": TYPE_BOTTLE,
}
SIDES = [SIDE_LEFT, SIDE_RIGHT]

Handler = Callable[[BabyDaybookCoordinator, str, dict[str, Any], datetime | None], Awaitable[str]]


# ----- the actions ------------------------------------------------------------


async def _log_diaper(coordinator, baby_uid, data, when) -> str:
    pee, poo = DIAPER_KINDS[data[ATTR_KIND]]
    return await coordinator.async_log_diaper(baby_uid, pee=pee, poo=poo, at=when)


async def _fell_asleep(coordinator, baby_uid, data, when) -> str:
    return await coordinator.async_set_sleep(baby_uid, True, when)


async def _woke_up(coordinator, baby_uid, data, when) -> str:
    return await coordinator.async_set_sleep(baby_uid, False, when)


async def _start_breastfeeding(coordinator, baby_uid, data, when) -> str:
    return await coordinator.async_set_breastfeeding(baby_uid, data[ATTR_SIDE], when)


async def _stop_breastfeeding(coordinator, baby_uid, data, when) -> str:
    return await coordinator.async_set_breastfeeding(baby_uid, None, when)


async def _log_bottle(coordinator, baby_uid, data, when) -> str:
    return await coordinator.async_log_bottle(
        baby_uid, amount_ml=data[ATTR_AMOUNT], at=when
    )


async def _correct_diaper(coordinator, baby_uid, data, when) -> str:
    pee, poo = DIAPER_KINDS[data[ATTR_KIND]] if ATTR_KIND in data else (None, None)
    return await coordinator.async_correct_diaper(baby_uid, pee=pee, poo=poo, at=when)


async def _add_note(coordinator, baby_uid, data, when) -> str:
    return await coordinator.async_add_note(
        baby_uid, ACTIVITIES[data[ATTR_ACTIVITY]], data[ATTR_NOTE]
    )


async def _delete_last(coordinator, baby_uid, data, when) -> str:
    return await coordinator.async_delete_last(baby_uid, ACTIVITIES[data[ATTR_ACTIVITY]])


_WHEN = (
    "For something that happened earlier, pass either minutes_ago or time "
    "(24-hour HH:MM, today or the most recent past occurrence), never both. "
    "Omit both for now. Pass baby when a name was said."
)

# name -> (handler, extra fields, takes a time, description for AI agents)
ACTIONS: dict[str, tuple[Handler, dict[Any, Any], bool, str]] = {
    "log_diaper": (
        _log_diaper,
        {vol.Required(ATTR_KIND): vol.In(list(DIAPER_KINDS))},
        True,
        ("Record a diaper change in Baby Daybook. kind is wet (pee), dirty "
        f"(poo) or mixed (both). {_WHEN}"),
    ),
    "fell_asleep": (
        _fell_asleep,
        {},
        True,
        ("Record in Baby Daybook that a baby fell asleep. If the sleep is "
        f"already running, a given time corrects when it started. {_WHEN}"),
    ),
    "woke_up": (
        _woke_up,
        {},
        True,
        ("Record in Baby Daybook that a baby woke up, ending the sleep. If the "
        "sleep was already ended, a given time corrects when it ended. "
        f"{_WHEN}"),
    ),
    "start_breastfeeding": (
        _start_breastfeeding,
        {vol.Required(ATTR_SIDE): vol.In(SIDES)},
        True,
        ("Record in Baby Daybook that breastfeeding started on the left or "
        "right side, or moved to that side if a feeding is running. "
        f"{_WHEN}"),
    ),
    "stop_breastfeeding": (
        _stop_breastfeeding,
        {},
        True,
        f"Record in Baby Daybook that breastfeeding ended. {_WHEN}",
    ),
    "log_bottle": (
        _log_bottle,
        {
            vol.Required(ATTR_AMOUNT): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=MAX_BOTTLE_ML)
            )
        },
        True,
        (
            "Record a finished bottle feed in Baby Daybook. amount is in "
            f"millilitres. {_WHEN}"
        ),
    ),
    "correct_diaper": (
        _correct_diaper,
        {vol.Optional(ATTR_KIND): vol.In(list(DIAPER_KINDS))},
        True,
        (
            "Correct the most recent diaper change in Baby Daybook: pass kind "
            "(wet, dirty or mixed) to change what it was, and/or minutes_ago "
            "or time (24-hour HH:MM) to change when it happened. Pass baby "
            "when a name was said."
        ),
    ),
    "add_note": (
        _add_note,
        {
            vol.Required(ATTR_ACTIVITY): vol.In(list(ACTIVITIES)),
            vol.Required(ATTR_NOTE): cv.string,
        },
        False,
        (
            "Write a note on the most recent Baby Daybook entry of one "
            "activity (diaper, sleep, breastfeeding or bottle), replacing any "
            "note it had. Pass baby when a name was said."
        ),
    ),
    "delete_last": (
        _delete_last,
        {vol.Required(ATTR_ACTIVITY): vol.In(list(ACTIVITIES))},
        False,
        ("Delete the most recent Baby Daybook entry of one activity (diaper, "
        "sleep, breastfeeding or bottle), to undo a mistake. Pass baby when a name "
        "was said."),
    ),
}

SPEECH = {
    OUTCOME_LOGGED: "Logged for {baby}{at}.",
    OUTCOME_STARTED: "Started for {baby}{at}.",
    OUTCOME_STOPPED: "Stopped for {baby}{at}.",
    OUTCOME_SWITCHED: "Switched side for {baby}{at}.",
    OUTCOME_CORRECTED: "Corrected for {baby}{at}.",
    OUTCOME_DELETED: "Deleted the last entry for {baby}.",
}


# ----- shared helpers -----------------------------------------------------


def _plain(name: str) -> str:
    """Reduce a name to its letters and digits, so emoji and case don't matter."""
    return re.sub(r"[\W_]+", "", name).casefold()


@callback
def resolve_baby(
    hass: HomeAssistant, name: str | None
) -> tuple[BabyDaybookCoordinator, str, str]:
    """Find a baby by (part of) its name; the name is optional with one baby."""
    candidates = [
        (entry.runtime_data, baby)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        for baby in entry.runtime_data.babies
    ]
    if not candidates:
        raise ServiceValidationError("Baby Daybook is not set up or not loaded")
    names = ", ".join(baby.name for _, baby in candidates)
    if not name or not _plain(name):
        if len(candidates) > 1:
            raise ServiceValidationError(f"Say which baby: {names}")
        matches = candidates
    else:
        wanted = _plain(name)
        matches = [c for c in candidates if _plain(c[1].name) == wanted or c[1].uid == name]
        if not matches:
            matches = [c for c in candidates if _plain(c[1].name).startswith(wanted)]
        if len(matches) != 1:
            raise ServiceValidationError(f"No baby called {name}. Known: {names}")
    coordinator, baby = matches[0]
    return coordinator, baby.uid, baby.name


@callback
def resolve_when(minutes_ago: float | None, at: datetime | time | str | None) -> datetime | None:
    """Turn "N minutes ago" or a clock time into a moment; None means now."""
    if minutes_ago is not None and at is not None:
        raise ServiceValidationError("Give minutes_ago or time, not both")
    now = dt_util.utcnow()
    if minutes_ago is not None:
        if minutes_ago < 0:
            raise ServiceValidationError("minutes_ago must not be negative")
        return now - timedelta(minutes=minutes_ago) if minutes_ago else None
    if at is None:
        return None
    if isinstance(at, str):
        text = at.strip()
        parsed = dt_util.parse_datetime(text) or dt_util.parse_time(text)
        if parsed is None:
            raise ServiceValidationError(f"Could not understand the time {at!r}")
        at = parsed
    if isinstance(at, time):
        # A bare clock time means its most recent occurrence.
        local_now = dt_util.now()
        moment = local_now.replace(
            hour=at.hour, minute=at.minute, second=at.second, microsecond=0
        )
        if moment > local_now + timedelta(minutes=1):
            moment -= timedelta(days=1)
        return dt_util.as_utc(moment)
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(at)


async def _run(hass: HomeAssistant, name: str, data: dict[str, Any]) -> tuple[str, str, datetime | None]:
    handler, _, timed, _ = ACTIONS[name]
    coordinator, baby_uid, baby_name = resolve_baby(hass, data.get(ATTR_BABY))
    when = resolve_when(data.get(ATTR_MINUTES_AGO), data.get(ATTR_TIME)) if timed else None
    return await handler(coordinator, baby_uid, data, when), baby_name, when


# ----- registration -------------------------------------------------------


@callback
def async_setup_actions(hass: HomeAssistant) -> None:
    """Register the services and the voice intents."""
    for name, (_, fields, timed, description) in ACTIONS.items():
        service_schema: dict[Any, Any] = {vol.Optional(ATTR_BABY): cv.string, **fields}
        intent_schema: dict[Any, Any] = {vol.Optional(ATTR_BABY): cv.string, **fields}
        if timed:
            service_schema[vol.Optional(ATTR_MINUTES_AGO)] = vol.All(
                vol.Coerce(float), vol.Range(min=0)
            )
            service_schema[vol.Optional(ATTR_TIME)] = vol.Any(cv.datetime, cv.time)
            intent_schema[vol.Optional(ATTR_MINUTES_AGO)] = vol.Coerce(float)
            intent_schema[vol.Optional(ATTR_TIME)] = cv.string

        async def handle_service(call: ServiceCall, name: str = name) -> None:
            await _run(hass, name, dict(call.data))

        hass.services.async_register(
            DOMAIN, name, handle_service, schema=vol.Schema(service_schema)
        )
        intent.async_register(hass, _ActionIntent(name, intent_schema, description))


class _ActionIntent(intent.IntentHandler):
    """One Baby Daybook action as a voice intent."""

    def __init__(self, name: str, schema: dict[Any, Any], description: str) -> None:
        """Initialise the intent."""
        self._name = name
        self._schema = schema
        self.intent_type = "BabyDaybook" + "".join(
            part.capitalize() for part in name.split("_")
        )
        self.description = description

    @property
    def slot_schema(self) -> dict[Any, Any]:
        """Return the slots this intent accepts."""
        return self._schema

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Run the action and say what happened."""
        slots = self.async_validate_slots(intent_obj.slots)
        data = {key: slot["value"] for key, slot in slots.items()}
        try:
            outcome, baby, when = await _run(intent_obj.hass, self._name, data)
        except ServiceValidationError as err:
            raise intent.IntentHandleError(str(err)) from err
        at = f" at {dt_util.as_local(when).strftime('%H:%M')}" if when else ""
        response = intent_obj.create_response()
        response.async_set_speech(
            SPEECH.get(outcome, "Nothing to change for {baby}.").format(baby=baby, at=at)
        )
        return response
