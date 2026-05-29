"""Unit tests for :mod:`ditto.api_server.pricing.errors`."""

from __future__ import annotations

import pytest

from ditto.api_server.pricing.errors import (
    MalformedPriceError,
    OracleUnreachableError,
    PriceTooStaleError,
    PricingError,
)


class TestHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [OracleUnreachableError, MalformedPriceError, PriceTooStaleError],
    )
    def test_inherits_from_base(self, cls):
        assert issubclass(cls, PricingError)
        assert issubclass(cls, Exception)


class TestDocstrings:
    @pytest.mark.parametrize(
        "cls",
        [OracleUnreachableError, MalformedPriceError, PriceTooStaleError],
    )
    def test_can_happen_when_present(self, cls):
        assert "This can happen when" in (cls.__doc__ or "")
