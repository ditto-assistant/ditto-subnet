"""Unit tests for ditto.chain.errors.

Every chain-layer error must inherit ``ChainError`` so consumers can write a
single ``except ChainError``.
"""

from __future__ import annotations

import pytest

from ditto.chain.errors import (
    ChainAuthError,
    ChainConnectionError,
    ChainError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
)

CHAIN_ERROR_SUBCLASSES = [
    ChainAuthError,
    ChainConnectionError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
]


@pytest.mark.parametrize("subclass", CHAIN_ERROR_SUBCLASSES)
def test_subclass_inherits_chain_error(subclass: type[Exception]) -> None:
    """Single ``except ChainError`` must catch every chain-layer failure."""
    assert issubclass(subclass, ChainError)
