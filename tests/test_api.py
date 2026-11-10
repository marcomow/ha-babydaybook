"""Tests for the cloud client, against an in-memory fake of Firebase."""

from __future__ import annotations

import pytest

from custom_components.babydaybook.api import (
    SIDE_LEFT,
    SIDE_RIGHT,
    TYPE_BREASTFEEDING,
    TYPE_SLEEP,
    Baby,
    BabyDaybookAuthError,
    BabyDaybookClient,
    decode_value,
    encode_value,
    running_action,
    stop_changes,
    switch_side_changes,
)

from .fake_cloud import USER_UID, FakeCloud


@pytest.fixture
def cloud() -> FakeCloud:
    fake = FakeCloud()
    fake.add_baby("baby1", "Robin")
    return fake


@pytest.fixture
def client(cloud: FakeCloud) -> BabyDaybookClient:
    return BabyDaybookClient(cloud, "refresh-1")  # type: ignore[arg-type]


def test_value_round_trip() -> None:
    for value in (None, True, False, 0, 42, 1.5, "x", {"a": [1, "b"]}):
        assert decode_value(encode_value(value)) == value
    # Booleans must not be written as integers, and integers travel as strings.
    assert encode_value(True) == {"booleanValue": True}
    assert encode_value(1) == {"integerValue": "1"}


async def test_refresh_token_login_and_baby_list(client: BabyDaybookClient) -> None:
    assert await client.authenticate() == USER_UID
    assert await client.list_babies() == [Baby(uid="baby1", name="Robin")]


async def test_invited_and_deleted_babies(cloud: FakeCloud, client: BabyDaybookClient) -> None:
    cloud.put(f"userData/{USER_UID}/acceptedInvites/x", {"babyUid": "baby2"})
    cloud.put("babyData/babyUid_baby2", {"uid": "baby2", "name": "Second"})
    cloud.put(f"userData/{USER_UID}/createdBabies/gone", {"babyUid": "gone", "deleted": 1})
    names = [baby.name for baby in await client.list_babies()]
    assert names == ["Robin", "Second"]


async def test_rejected_refresh_token_is_an_auth_error(cloud: FakeCloud) -> None:
    client = BabyDaybookClient(cloud, "stale")  # type: ignore[arg-type]
    with pytest.raises(BabyDaybookAuthError):
        await client.authenticate()


async def test_password_login(cloud: FakeCloud) -> None:
    client = await BabyDaybookClient.sign_in_with_password(cloud, "a@b.c", "correct")  # type: ignore[arg-type]
    assert client.refresh_token == "refresh-1"
    assert client.user_uid == USER_UID
    with pytest.raises(BabyDaybookAuthError):
        await BabyDaybookClient.sign_in_with_password(cloud, "a@b.c", "wrong")  # type: ignore[arg-type]


async def test_log_diaper_writes_a_native_shaped_record(
    cloud: FakeCloud, client: BabyDaybookClient
) -> None:
    record = await client.log_diaper("baby1", pee=True, poo=False)
    (stored,) = cloud.actions("baby1")
    assert stored["type"] == "diaper_change"
    assert (stored["pee"], stored["poo"], stored["inProgress"]) == (1, 0, 0)
    assert stored["userUid"] == USER_UID
    assert stored["babyUid"] == "baby1"
    assert stored["rev"] == 4
    assert len(stored["uid"]) == 32 and stored["uid"].isalnum()
    assert stored["uid"] == record["uid"]
    assert "svt" in stored  # stamped by the server, which is what the phones sync on


async def test_sleep_start_and_stop(cloud: FakeCloud, client: BabyDaybookClient) -> None:
    started = await client.start_timer("baby1", TYPE_SLEEP)
    assert cloud.actions("baby1")[0]["inProgress"] == 1
    stopped = await client.stop_timer(started)
    (stored,) = cloud.actions("baby1")
    assert stored["inProgress"] == 0
    assert stored["endMillis"] >= stored["startMillis"]
    assert stored["duration"] == stored["endMillis"] - stored["startMillis"]
    assert stored["type"] == TYPE_SLEEP  # untouched fields survive the merge
    assert stopped["inProgress"] == 0


async def test_recent_and_changed_queries(cloud: FakeCloud, client: BabyDaybookClient) -> None:
    await client.log_diaper("baby1", pee=True, poo=False, at_millis=1000)
    await client.log_diaper("baby1", pee=False, poo=True, at_millis=2000)
    recent = await client.recent_actions("baby1", limit=1)
    assert [action["startMillis"] for action in recent] == [2000]
    cursor = recent[0]["_svt"]
    assert await client.changed_actions("baby1", cursor) == []
    await client.start_timer("baby1", TYPE_SLEEP)
    changed = await client.changed_actions("baby1", cursor)
    assert [action["type"] for action in changed] == [TYPE_SLEEP]


def test_breastfeeding_side_accounting() -> None:
    action = {"type": TYPE_BREASTFEEDING, "side": SIDE_LEFT, "startMillis": 0, "inProgress": 1}
    action |= switch_side_changes(action, SIDE_RIGHT, 60_000)
    assert (action["leftDuration"], action["rightDuration"]) == (60_000, 0)
    action |= stop_changes(action, 100_000)
    assert (action["leftDuration"], action["rightDuration"]) == (60_000, 40_000)
    assert action["duration"] == 100_000


def test_running_action_ignores_deleted_and_finished() -> None:
    actions = [
        {"uid": "a", "type": TYPE_SLEEP, "startMillis": 1, "inProgress": 1, "deleted": 1},
        {"uid": "b", "type": TYPE_SLEEP, "startMillis": 2, "inProgress": 0},
        {"uid": "c", "type": TYPE_SLEEP, "startMillis": 3, "inProgress": True},
    ]
    assert running_action(actions, TYPE_SLEEP)["uid"] == "c"
    assert running_action(actions[:2], TYPE_SLEEP) is None
