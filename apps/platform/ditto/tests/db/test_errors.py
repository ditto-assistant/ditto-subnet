"""Database error hierarchy tests."""

from __future__ import annotations

import pytest

from ditto.db.errors import (
    DatabaseConnectionError,
    DatabaseError,
    IntegrityError,
    QueryError,
)

_SUBCLASSES = [DatabaseConnectionError, QueryError, IntegrityError]


class TestErrorHierarchy:
    """Every domain error must inherit from :class:`DatabaseError`."""

    @pytest.mark.parametrize("cls", _SUBCLASSES)
    def test_inherits_from_database_error(self, cls: type[Exception]):
        assert issubclass(cls, DatabaseError)
