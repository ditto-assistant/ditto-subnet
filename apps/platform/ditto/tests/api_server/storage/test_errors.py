"""Storage error hierarchy tests."""

from __future__ import annotations

import pytest

from ditto.api_server.storage import (
    ObjectDownloadTooLargeError,
    ObjectUploadFailedError,
    StorageConfigurationError,
    StorageError,
)


class TestHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [
            ObjectUploadFailedError,
            ObjectDownloadTooLargeError,
            StorageConfigurationError,
        ],
    )
    def test_inherits_from_base(self, cls):
        assert issubclass(cls, StorageError)
