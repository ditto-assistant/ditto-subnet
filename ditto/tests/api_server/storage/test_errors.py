"""Each StorageError subclass must document its trigger conditions."""

from __future__ import annotations

import inspect

import pytest

from ditto.api_server.storage import (
    ObjectUploadFailedError,
    StorageConfigurationError,
    StorageError,
)


class TestErrorDocstrings:
    @pytest.mark.parametrize(
        "cls",
        [ObjectUploadFailedError, StorageConfigurationError],
    )
    def test_has_trigger_bullets(self, cls: type[Exception]):
        doc = inspect.cleandoc(cls.__doc__ or "")
        assert "This can happen when:" in doc, (
            f"{cls.__name__} missing 'This can happen when:' docstring section"
        )
        assert "\n- " in doc, (
            f"{cls.__name__} 'This can happen when:' has no bullet entries"
        )


class TestHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [ObjectUploadFailedError, StorageConfigurationError],
    )
    def test_inherits_from_base(self, cls):
        assert issubclass(cls, StorageError)
        assert issubclass(cls, Exception)
