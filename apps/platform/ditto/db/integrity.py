"""Name the exact constraint behind a database integrity error.

A handler may turn a uniqueness conflict into a precise refusal only when it
is the conflict that handler's own write can cause. Every other integrity
failure is a bug and must surface as one, not as a misleading 409.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

UNIQUE_VIOLATION = "23505"


def unique_violation(exc: IntegrityError) -> str | None:
    """The violated unique constraint or index, or ``None`` for anything else.

    SQLAlchemy's asyncpg adapter chains the driver error, which carries the
    SQLSTATE and the constraint name, as ``exc.orig.__cause__``.
    """
    driver_error = getattr(exc.orig, "__cause__", None)
    if getattr(driver_error, "sqlstate", None) != UNIQUE_VIOLATION:
        return None
    return getattr(driver_error, "constraint_name", None)
