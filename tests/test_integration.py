"""End-to-end tests of the integration inside Home Assistant."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.babydaybook.api import TYPE_BREASTFEEDING, TYPE_SLEEP
from custom_components.babydaybook.const import CONF_REFRESH_TOKEN, DOMAIN

from .fake_cloud import USER_UID, FakeCloud

SLEEP = "switch.robin_sleep"
LEFT = "switch.robin_breastfeeding_left"
RIGHT = "switch.robin_breastfeeding_right"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_UID, data={CONF_REFRESH_TOKEN: "refresh-1"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)


async def _call(hass: HomeAssistant, domain: str, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        domain, service, {"entity_id": entity_id}, blocking=True
    )


async def test_config_flow_with_password(hass: HomeAssistant, cloud: FakeCloud) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "sign_in"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "parent@example.com", "password": "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "parent@example.com", "password": "correct"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Only the session token is stored, never the password.
    assert result["data"] == {CONF_REFRESH_TOKEN: "refresh-1"}
    assert result["result"].unique_id == USER_UID


async def test_config_flow_set_password_first(hass: HomeAssistant, cloud: FakeCloud) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "set_password"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "nobody@example.com"}
    )
    assert result["errors"] == {"base": "email_not_found"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "parent@example.com"}
    )
    assert result["step_id"] == "sign_in"
    assert cloud.count("accounts:sendOobCode") == 2


async def test_diaper_buttons(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    assert hass.states.get("sensor.robin_last_diaper_change").state == "unknown"
    await _call(hass, "button", "press", "button.robin_diaper_dirty")
    state = hass.states.get("sensor.robin_last_diaper_change")
    assert state.state not in ("unknown", "unavailable")
    assert (state.attributes["wet"], state.attributes["dirty"]) == (False, True)
    await _call(hass, "button", "press", "button.robin_diaper_wet")
    await _call(hass, "button", "press", "button.robin_diaper_wet_and_dirty")
    flags = sorted((a["pee"], a["poo"]) for a in cloud.actions("baby1"))
    assert flags == [(0, 1), (1, 0), (1, 1)]


async def test_sleep_switch_is_idempotent(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    assert hass.states.get(SLEEP).state == "off"
    await _call(hass, "switch", "turn_on", SLEEP)
    await _call(hass, "switch", "turn_on", SLEEP)
    assert hass.states.get(SLEEP).state == "on"
    assert len(cloud.actions("baby1")) == 1
    await _call(hass, "switch", "turn_off", SLEEP)
    await _call(hass, "switch", "turn_off", SLEEP)
    assert hass.states.get(SLEEP).state == "off"
    (sleep,) = cloud.actions("baby1")
    assert sleep["inProgress"] == 0 and sleep["endMillis"] > 0


async def test_changes_made_on_a_phone_show_up(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _call(hass, "button", "press", "button.robin_diaper_wet")  # gives a sync cursor
    path = "babyData/babyUid_baby1/dailyActions/fromphone"
    record = {"uid": "fromphone", "babyUid": "baby1", "type": TYPE_SLEEP,
              "startMillis": 4_000_000_000_000, "inProgress": 1}
    cloud.put(path, record, stamp=True)
    await _poll(hass)
    assert hass.states.get(SLEEP).state == "on"

    # Stopping from Home Assistant ends the phone's timer, not a new one.
    await _call(hass, "switch", "turn_off", SLEEP)
    assert cloud.get(path)["inProgress"] == 0
    assert len(cloud.actions("baby1")) == 2


async def test_phone_change_seen_before_acting(hass: HomeAssistant, cloud: FakeCloud) -> None:
    """A sleep started on a phone seconds ago must not be duplicated."""
    await _setup(hass)
    await _call(hass, "button", "press", "button.robin_diaper_wet")
    cloud.put(
        "babyData/babyUid_baby1/dailyActions/fromphone",
        {"uid": "fromphone", "babyUid": "baby1", "type": TYPE_SLEEP,
         "startMillis": 4_000_000_000_000, "inProgress": 1},
        stamp=True,
    )
    await _call(hass, "switch", "turn_on", SLEEP)
    assert len([a for a in cloud.actions("baby1") if a["type"] == TYPE_SLEEP]) == 1


async def test_breastfeeding_sides(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _call(hass, "switch", "turn_on", LEFT)
    assert (hass.states.get(LEFT).state, hass.states.get(RIGHT).state) == ("on", "off")
    await _call(hass, "switch", "turn_on", RIGHT)  # switches side, same session
    assert (hass.states.get(LEFT).state, hass.states.get(RIGHT).state) == ("off", "on")
    await _call(hass, "switch", "turn_off", LEFT)  # not the active side: ignored
    assert hass.states.get(RIGHT).state == "on"
    await _call(hass, "switch", "turn_off", RIGHT)
    (feed,) = cloud.actions("baby1")
    assert feed["type"] == TYPE_BREASTFEEDING and feed["inProgress"] == 0
    assert feed["leftDuration"] + feed["rightDuration"] == feed["duration"]


async def test_polling_is_cheap_and_falls_back(hass: HomeAssistant, cloud: FakeCloud) -> None:
    await _setup(hass)
    await _call(hass, "button", "press", "button.robin_diaper_wet")
    before = len(cloud.requests)
    await _poll(hass)
    assert len(cloud.requests) - before == 1  # one incremental query, nothing else

    cloud.reject_delta_queries = True
    await _poll(hass)
    await _poll(hass)
    assert hass.states.get(SLEEP).state == "off"  # still healthy via full reloads


async def test_revoked_session_starts_reauth(hass: HomeAssistant, cloud: FakeCloud) -> None:
    cloud.refresh_token = "rotated-elsewhere"
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_UID, data={CONF_REFRESH_TOKEN: "refresh-1"}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())
