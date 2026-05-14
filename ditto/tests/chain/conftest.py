"""Shared fixtures for ditto.chain tests."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_pylon() -> AsyncMock:
    """Build an AsyncMock that mimics ``AsyncPylonClient``.

    Tests can override individual methods on
    ``mock_pylon.v1.open_access.<method>`` or ``mock_pylon.identity.<method>``.
    """
    pylon = AsyncMock()
    pylon.__aenter__.return_value = pylon
    pylon.__aexit__.return_value = None
    pylon.v1 = MagicMock()
    pylon.v1.open_access = MagicMock()
    pylon.v1.open_access.get_recent_neurons = AsyncMock(return_value=[])
    pylon.v1.open_access.get_latest_block = AsyncMock(
        return_value=MagicMock(number=0, hash="0x00", timestamp=0)
    )
    pylon.v1.open_access.get_extrinsic = AsyncMock()
    pylon.identity = MagicMock()
    pylon.identity.put_weights = AsyncMock(return_value=None)
    return pylon


@pytest.fixture
def mock_substrate() -> AsyncMock:
    """Build an AsyncMock that mimics ``AsyncSubstrateInterface``."""
    substrate = AsyncMock()
    substrate.__aenter__.return_value = substrate
    substrate.__aexit__.return_value = None
    substrate.query = AsyncMock()
    return substrate


@pytest.fixture
def install_pylon_module(monkeypatch: pytest.MonkeyPatch, mock_pylon: AsyncMock) -> AsyncMock:
    """Install a fake ``pylon_client`` module whose ``AsyncPylonClient`` returns the mock."""
    module = MagicMock()
    module.AsyncPylonClient = MagicMock(return_value=mock_pylon)
    monkeypatch.setitem(sys.modules, "pylon_client", module)
    return mock_pylon


@pytest.fixture
def install_substrate_module(
    monkeypatch: pytest.MonkeyPatch, mock_substrate: AsyncMock
) -> AsyncMock:
    """Install a fake ``async_substrate_interface`` module whose class returns the mock."""
    module = MagicMock()
    module.AsyncSubstrateInterface = MagicMock(return_value=mock_substrate)
    monkeypatch.setitem(sys.modules, "async_substrate_interface", module)
    return mock_substrate


def make_event_record(
    extrinsic_index: int,
    module_id: str = "System",
    event_id: str = "ExtrinsicSuccess",
) -> dict[str, Any]:
    """Build one event-record dict in the shape async-substrate-interface returns."""
    return {
        "phase": {"ApplyExtrinsic": extrinsic_index},
        "event": {"module_id": module_id, "event_id": event_id},
    }
