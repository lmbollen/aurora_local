"""Fixtures for tests that run the integration inside Home Assistant."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

DEVICE_KEY = "AABBCCDDEEFF"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_network():
    """The client never connects in these tests."""
    with (
        patch("custom_components.aurora_local.coordinator.AdlarClient.start"),
        patch("custom_components.aurora_local.coordinator.AdlarClient.stop", new=AsyncMock()),
    ):
        yield
