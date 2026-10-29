"""The persisted failure-class allowlist must equal the shared wire allowlist.

These two lists are edited in different languages, in different directories,
for different reasons -- one is a CHECK constraint carried forward by an
immutable migration, the other is the tuple a reporter signs against. Drift
between them is invisible until a validator hands back a failure, at which
point the request validates and the write is refused, or the write would be
accepted and the request is not. Either way the diagnostic is lost at exactly
the moment it exists, which is the failure mode the forwarded class was added
to remove.

A migration must not import the growing tuple (it would rewrite its own history
the next time the scorer gains a kind), so this test is what keeps the head
schema and the contract in step instead.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ditto_screening_protocol.confirmation_transport import (
    CONFIRMATION_EXECUTOR_FAILURE_CLASS_VALUES,
    CONFIRMATION_FAILURE_CLASS_VALUES,
    CONFIRMATION_HARNESS_FAILURE_CLASS_VALUES,
    CONFIRMATION_SCORER_FAILURE_CLASS_VALUES,
)

_TABLE = "confirmation_bundle_tickets"


async def _check_definition(engine: AsyncEngine, *required: str) -> str:
    """The one CHECK on the tickets table whose body contains every fragment.

    Selected by definition rather than by name: the metadata naming convention
    rewrites every check into ``ck_<table>_<truncated>_<hash>``, so the logical
    name in the migration is not the name in the catalog, and a lookup by it
    silently returns nothing -- which a test can easily read as "the constraint
    does not allow my value" instead of "I asked the wrong question".
    """
    async with engine.connect() as connection:
        definitions = (
            await connection.execute(
                text(
                    "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = :table AND c.contype = 'c'"
                ),
                {"table": _TABLE},
            )
        ).scalars()
        matches = [
            definition
            for definition in definitions
            if all(fragment in definition for fragment in required)
        ]
    assert len(matches) == 1, (
        f"expected exactly one CHECK on {_TABLE} containing {required!r}, "
        f"found {len(matches)}"
    )
    return str(matches[0])


@pytest.mark.asyncio
async def test_failure_class_constraint_matches_the_shared_allowlist(
    engine: AsyncEngine,
) -> None:
    definition = await _check_definition(engine, "failure_class = ANY")
    stored = set(re.findall(r"'([a-z0-9_]+)'::text", definition))
    assert stored == set(CONFIRMATION_FAILURE_CLASS_VALUES)


@pytest.mark.asyncio
async def test_the_scorer_classes_are_actually_present(engine: AsyncEngine) -> None:
    """Guards the guard: a regex that matched nothing would pass the test above.

    An empty ``stored`` set compared against an empty allowlist is a vacuous
    pass, and the allowlist is never empty -- so assert each scorer slug is
    individually there, which cannot hold unless both the parse and the
    migration worked.
    """
    definition = await _check_definition(engine, "failure_class = ANY")
    # Both families must be individually present. Asserting a count here would
    # only restate the tuple and would have to be edited every time the scorer
    # gains a kind -- which is the edit most likely to be made without checking
    # the schema, i.e. exactly the drift this file exists to catch.
    assert (
        *CONFIRMATION_HARNESS_FAILURE_CLASS_VALUES,
        *CONFIRMATION_EXECUTOR_FAILURE_CLASS_VALUES,
    ) == CONFIRMATION_SCORER_FAILURE_CLASS_VALUES
    assert CONFIRMATION_HARNESS_FAILURE_CLASS_VALUES
    assert CONFIRMATION_EXECUTOR_FAILURE_CLASS_VALUES
    missing = [
        value
        for value in CONFIRMATION_SCORER_FAILURE_CLASS_VALUES
        if f"'{value}'" not in definition
    ]
    assert missing == []


@pytest.mark.asyncio
async def test_failure_status_is_bounded_and_requires_a_class(
    engine: AsyncEngine,
) -> None:
    definition = await _check_definition(engine, "failure_status")
    assert "failure_class IS NOT NULL" in definition
    assert "0" in definition and "599" in definition
