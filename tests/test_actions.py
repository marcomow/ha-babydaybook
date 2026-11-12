"""Tests for backdated entries, corrections, services and voice intents."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import intent, llm
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.babydaybook.actions import resolve_when
from custom_components.babydaybook.api import TYPE_BREASTFEEDING, TYPE_SLEEP
from custom_components.babydaybook.const import DOMAIN

from .fake_cloud import USER_UID, FakeCloud
from .test_integration import SLEEP, _setup

MINUTE = 60_000


def _now() -> int:
    return int(time.time() * 1000)


def _about(actual: int, expected: int) -> bool:
    return abs(actual - expected) < 5_000


async def _service(hass: HomeAssistant, name: str, **data: object) -> None:
    await hass.services.async_call(DOMAIN, name, data, blocking=True)


def _of_type(cloud: FakeCloud, action_type: str) -> list[dict]:
    return [a for a in cloud.actions("baby1") if a["type"] == action_type and not a.get("deleted")]


async def test_diaper_logged_in_the_past(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _service(hass, "log_diaper", kind="mixed", minutes_ago=25)
    (diaper,) = cloud.actions("baby1")
    assert (diaper["pee"], diaper["poo"]) == (1, 1)
    assert _about(diaper["startMillis"], _now() - 25 * MINUTE)


async def test_woke_up_ten_minutes_ago(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _service(hass, "fell_asleep", minutes_ago=90)
    assert hass.states.get(SLEEP).state == "on"
    await _service(hass, "woke_up", minutes_ago=10)
    assert hass.states.get(SLEEP).state == "off"
    (sleep,) = _of_type(cloud, TYPE_SLEEP)
    assert _about(sleep["startMillis"], _now() - 90 * MINUTE)
    assert _about(sleep["endMillis"], _now() - 10 * MINUTE)
    assert sleep["duration"] == sleep["endMillis"] - sleep["startMillis"]
    assert sleep["inProgress"] == 0


async def test_times_correct_existing_sleep(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _service(hass, "fell_asleep")
    await _service(hass, "fell_asleep", minutes_ago=40)  # running: moves the start
    (sleep,) = _of_type(cloud, TYPE_SLEEP)
    assert _about(sleep["startMillis"], _now() - 40 * MINUTE) and sleep["inProgress"] == 1

    await _service(hass, "woke_up")
    await _service(hass, "woke_up", minutes_ago=15)  # already ended: moves the end
    (sleep,) = _of_type(cloud, TYPE_SLEEP)
    assert _about(sleep["endMillis"], _now() - 15 * MINUTE)
    assert sleep["duration"] == sleep["endMillis"] - sleep["startMillis"]

    await _service(hass, "woke_up")  # no time, nothing running: nothing happens
    assert len(_of_type(cloud, TYPE_SLEEP)) == 1


async def test_impossible_times_are_refused(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _service(hass, "fell_asleep", minutes_ago=20)
    with pytest.raises(ServiceValidationError):  # woke before falling asleep
        await _service(hass, "woke_up", minutes_ago=30)
    with pytest.raises(ServiceValidationError):  # both ways of giving a time
        await _service(hass, "woke_up", minutes_ago=5, time="10:00")
    assert hass.states.get(SLEEP).state == "on"

    await _service(hass, "woke_up", minutes_ago=10)
    # An old finished sleep must not be stretched by a late "woke up".
    path = next(p for p in cloud.documents if "/dailyActions/" in p)
    old = cloud.get(path)
    cloud.put(path, {**old, "startMillis": old["startMillis"] - 600 * MINUTE,
                     "endMillis": old["endMillis"] - 600 * MINUTE}, stamp=True)
    with pytest.raises(ServiceValidationError):
        await _service(hass, "woke_up", minutes_ago=1)


async def test_breastfeeding_services(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _service(hass, "start_breastfeeding", side="left", minutes_ago=20)
    await _service(hass, "start_breastfeeding", side="right", minutes_ago=8)
    await _service(hass, "stop_breastfeeding", minutes_ago=2)
    (feed,) = _of_type(cloud, TYPE_BREASTFEEDING)
    assert _about(feed["leftDuration"], 12 * MINUTE)
    assert _about(feed["rightDuration"], 6 * MINUTE)
    assert feed["leftDuration"] + feed["rightDuration"] == feed["duration"]

    await _service(hass, "stop_breastfeeding", minutes_ago=1)  # correct the end
    (feed,) = _of_type(cloud, TYPE_BREASTFEEDING)
    assert _about(feed["rightDuration"], 7 * MINUTE)
    assert feed["leftDuration"] + feed["rightDuration"] == feed["duration"]


async def test_delete_last(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _service(hass, "log_diaper", kind="wet", minutes_ago=30)
    await _service(hass, "log_diaper", kind="dirty")
    await _service(hass, "delete_last", activity="diaper")
    remaining = [a for a in cloud.actions("baby1") if not a.get("deleted")]
    assert [(a["pee"], a["poo"]) for a in remaining] == [(1, 0)]
    assert hass.states.get("sensor.robin_last_diaper_change").attributes["wet"] is True


async def test_baby_is_matched_by_name(hass: HomeAssistant, cloud: FakeCloud) -> None:
    cloud.put(f"userData/{USER_UID}/createdBabies/baby2", {"babyUid": "baby2"})
    cloud.put("babyData/babyUid_baby2", {"uid": "baby2", "name": "Mila \U0001f33a"})
    await _setup(hass)
    with pytest.raises(ServiceValidationError, match="which baby"):
        await _service(hass, "log_diaper", kind="wet")
    with pytest.raises(ServiceValidationError, match="No baby called"):
        await _service(hass, "log_diaper", kind="wet", baby="Zoe")
    await _service(hass, "log_diaper", kind="wet", baby="mila")  # emoji and case ignored
    assert len(cloud.actions("baby2")) == 1 and not cloud.actions("baby1")


async def test_voice_intent(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _service(hass, "fell_asleep", minutes_ago=60)
    response = await intent.async_handle(
        hass, "test", "BabyDaybookWokeUp",
        {"baby": {"value": "Robin"}, "minutes_ago": {"value": 10}},
    )
    assert response.speech["plain"]["speech"].startswith("Stopped for Robin at ")
    (sleep,) = _of_type(cloud, TYPE_SLEEP)
    assert _about(sleep["endMillis"], _now() - 10 * MINUTE)

    with pytest.raises(intent.IntentHandleError):
        await intent.async_handle(
            hass, "test", "BabyDaybookLogDiaper",
            {"kind": {"value": "wet"}, "baby": {"value": "Nobody"}},
        )


async def test_intents_are_offered_to_ai_agents(hass: HomeAssistant, cloud: FakeCloud) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    await _setup(hass)
    api = await llm.async_get_api(
        hass, llm.LLM_API_ASSIST,
        llm.LLMContext(platform="test", context=None, language="en",
                       assistant="conversation", device_id=None),
    )
    tools = {tool.name: tool for tool in api.tools}
    for name in ("BabyDaybookLogDiaper", "BabyDaybookFellAsleep", "BabyDaybookWokeUp",
                 "BabyDaybookStartBreastfeeding", "BabyDaybookStopBreastfeeding",
                 "BabyDaybookDeleteLast"):
        assert name in tools
    assert "minutes_ago" in tools["BabyDaybookWokeUp"].description


def test_clock_times_mean_the_most_recent_occurrence(hass: HomeAssistant) -> None:
    now = dt_util.now()
    earlier = (now - timedelta(hours=2)).strftime("%H:%M")
    later = (now + timedelta(hours=2)).strftime("%H:%M")
    assert timedelta(hours=1, minutes=58) < dt_util.utcnow() - resolve_when(None, earlier) < timedelta(hours=2, minutes=2)
    assert timedelta(hours=21, minutes=58) < dt_util.utcnow() - resolve_when(None, later) < timedelta(hours=22, minutes=2)
    assert resolve_when(None, None) is None and resolve_when(0, None) is None
