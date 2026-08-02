"""Storage error hierarchy tests."""

from __future__ import annotations

import pytest

from ditto.api_server.storage import (
    ObjectUploadFailedError,
    StorageConfigurationError,
    StorageError,
)


class TestHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [ObjectUploadFailedError, StorageConfigurationError],
    )
    def test_inherits_from_base(self, cls):
        assert issubclass(cls, StorageError)
