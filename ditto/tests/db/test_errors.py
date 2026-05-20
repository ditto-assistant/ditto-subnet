"""Convention-enforcement tests for ditto.db.errors.

Every subclass must derive from :class:`DatabaseError` and carry the
"This can happen when:" bullet list that other modules rely on for
operator-facing diagnostics. New subclasses added to ``ditto.db.errors``
should be appended to ``_SUBCLASSES`` so this checks them too.
"""

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


class TestErrorDocstringConvention:
    """Every subclass must carry the standard 'This can happen when:' block."""

    @pytest.mark.parametrize("cls", _SUBCLASSES)
    def test_has_this_can_happen_when_section(self, cls: type[Exception]):
        doc = cls.__doc__ or ""
        assert "This can happen when:" in doc, (
            f"{cls.__name__} missing 'This can happen when:' section"
        )

    @pytest.mark.parametrize("cls", _SUBCLASSES)
    def test_has_at_least_two_bullets(self, cls: type[Exception]):
        doc = cls.__doc__ or ""
        bullets = [line for line in doc.splitlines() if line.strip().startswith("-")]
        assert len(bullets) >= 2, (
            f"{cls.__name__} should list at least two example causes"
        )
