"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from .fake_cloud import FakeCloud


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from custom_components."""


@pytest.fixture
def cloud() -> Iterator[FakeCloud]:
    """Route the integration's HTTP calls to an in-memory fake of Firebase."""
    fake = FakeCloud()
    fake.add_baby("baby1", "Robin")
    with (
        patch(
            "custom_components.babydaybook.async_get_clientsession",
            return_value=fake,
        ),
        patch(
            "custom_components.babydaybook.config_flow.async_get_clientsession",
            return_value=fake,
        ),
    ):
        yield fake
