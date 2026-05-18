"""Unit tests for ditto.chain.errors.

Scope is convention enforcement: every chain-layer error must inherit
``ChainError`` (so consumers can write a single ``except ChainError``) and
every subclass must carry the ``"This can happen when:"`` docstring bullet
list (so future maintainers know the failure modes). Tests of Python
exception machinery itself are intentionally absent.
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


@pytest.mark.parametrize("subclass", CHAIN_ERROR_SUBCLASSES)
def test_subclass_documents_failure_modes(subclass: type[Exception]) -> None:
    """Each subclass must list its specific failure modes in its docstring.

    This is a convention check - new subclasses added without the bullet list
    fail here, forcing the author to document when the error can fire.
    """
    assert subclass.__doc__ is not None
    assert "This can happen when" in subclass.__doc__
