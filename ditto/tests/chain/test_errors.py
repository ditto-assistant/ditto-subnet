"""Unit tests for ditto.chain.errors."""

from __future__ import annotations

import pytest

from ditto.chain.errors import (
    ChainConnectionError,
    ChainError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
)


class TestChainErrorHierarchy:
    def test_chain_error_is_exception(self):
        assert issubclass(ChainError, Exception)

    @pytest.mark.parametrize(
        "subclass",
        [ChainConnectionError, ExtrinsicNotFoundError, ChainTimeoutError],
    )
    def test_subclasses_inherit_chain_error(self, subclass):
        assert issubclass(subclass, ChainError)

    def test_can_be_raised_and_caught_as_chain_error(self):
        with pytest.raises(ChainError):
            raise ChainConnectionError("pylon down")

    def test_error_message_preserved(self):
        with pytest.raises(ChainConnectionError) as info:
            raise ChainConnectionError("pylon down")
        assert "pylon down" in str(info.value)


class TestChainErrorDocstrings:
    @pytest.mark.parametrize(
        "subclass",
        [ChainConnectionError, ExtrinsicNotFoundError, ChainTimeoutError],
    )
    def test_subclass_has_this_can_happen_when(self, subclass):
        assert subclass.__doc__ is not None
        assert "This can happen when" in subclass.__doc__
