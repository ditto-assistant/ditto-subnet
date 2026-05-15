"""Shared fixtures for ditto.chain tests."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_pylon() -> AsyncMock:
    """Build an AsyncMock that mimics ``AsyncPylonClient``.

    Mirrors the real path: ``client.v1.open_access.<method>`` and
    ``client.v1.identity.<method>``. Tests can override individual
    methods on the fixture before entering ``async with``.
    """
    pylon = AsyncMock()
    pylon.__aenter__.return_value = pylon
    pylon.__aexit__.return_value = None
    pylon.v1 = MagicMock()
    pylon.v1.open_access = MagicMock()
    pylon.v1.open_access.get_recent_neurons = AsyncMock(
        return_value=MagicMock(neurons={}, block=MagicMock(number=0, hash="0x00"))
    )
    pylon.v1.open_access.get_latest_block_info = AsyncMock(
        return_value=MagicMock(number=0, hash="0x00", timestamp=0)
    )
    pylon.v1.open_access.get_extrinsic = AsyncMock()
    pylon.v1.identity = MagicMock()
    pylon.v1.identity.put_weights = AsyncMock(return_value=None)
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
def install_pylon_module(
    monkeypatch: pytest.MonkeyPatch, mock_pylon: AsyncMock
) -> AsyncMock:
    """Install a fake ``pylon_client.artanis`` module returning ``mock_pylon``."""
    artanis = MagicMock()
    artanis.AsyncPylonClient = MagicMock(return_value=mock_pylon)
    artanis.AsyncConfig = MagicMock(side_effect=lambda **kwargs: MagicMock(**kwargs))
    # Typed exception stand-ins for _translate_pylon_error fallback path.
    artanis.PylonNotFound = type("PylonNotFound", (Exception,), {})
    artanis.PylonTimeoutException = type("PylonTimeoutException", (Exception,), {})
    artanis.PylonClosed = type("PylonClosed", (Exception,), {})
    parent = MagicMock()
    parent.artanis = artanis
    monkeypatch.setitem(sys.modules, "pylon_client", parent)
    monkeypatch.setitem(sys.modules, "pylon_client.artanis", artanis)
    return mock_pylon


@pytest.fixture
def install_substrate_module(
    monkeypatch: pytest.MonkeyPatch, mock_substrate: AsyncMock
) -> AsyncMock:
    """Install a fake ``async_substrate_interface`` module."""
    module = MagicMock()
    module.AsyncSubstrateInterface = MagicMock(return_value=mock_substrate)
    monkeypatch.setitem(sys.modules, "async_substrate_interface", module)
    return mock_substrate


def make_event_record(
    extrinsic_index: int | None,
    module_id: str = "System",
    event_id: str = "ExtrinsicSuccess",
    phase: str = "ApplyExtrinsic",
) -> dict[str, Any]:
    """Build one event-record dict in the shape async-substrate-interface returns.

    Verified against finney's ``System.Events`` storage on mainnet: records
    are flat dicts with ``phase`` (str), ``extrinsic_idx`` (int or None for
    Initialization/Finalization phases), ``module_id``, ``event_id``, plus
    nested ``event`` data we don't read in ``check_extrinsic_success``.
    """
    return {
        "phase": phase,
        "extrinsic_idx": extrinsic_index,
        "module_id": module_id,
        "event_id": event_id,
        "event": {"module_id": module_id, "event_id": event_id, "attributes": []},
    }
