"""Unit tests for :mod:`ditto.api_server.errors`.

Checks both the inheritance hierarchy and the "This can happen when:"
bullet lists required by the code quality standards.
"""

from __future__ import annotations

import pytest

from ditto.api_server.errors import (
    ApiServerConfigError,
    ApiServerError,
    ApiServerLifespanError,
)


class TestHierarchy:
    """Every concrete error inherits from :class:`ApiServerError`."""

    @pytest.mark.parametrize("cls", [ApiServerConfigError, ApiServerLifespanError])
    def test_inherits_from_base(self, cls):
        assert issubclass(cls, ApiServerError)
        assert issubclass(cls, Exception)


class TestDocstrings:
    """Every concrete error documents its trigger surface."""

    @pytest.mark.parametrize("cls", [ApiServerConfigError, ApiServerLifespanError])
    def test_can_happen_when_present(self, cls):
        doc = cls.__doc__ or ""
        assert "This can happen when" in doc
