"""Unit tests for :mod:`ditto.api_server.errors`.

Checks the public inheritance hierarchy used by catch-all error handling.
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
