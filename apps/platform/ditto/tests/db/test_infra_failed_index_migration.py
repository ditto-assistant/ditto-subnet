"""The infra-failure partial index follows the retried reason codes (#2444, #2449).

Each upgrade must rebuild ``screening_attempts_infra_failed_idx`` over every
retried code; each downgrade must restore the prior predicate. Each direction
leaves a valid index and can run again from its own result.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ditto.db.queries.screening_infra_retry import INFRA_AUTO_RETRY_REASON_CODES

# (revision to downgrade to, the code the next revision added, the codes it kept)
_WIDENINGS = (
    (
        "e0f28816bca9",
        "l2-runtime-evidence-unavailable",
        ("docker-build-infrastructure", "worker-claim-not-started"),
    ),
    (
        "5e2a8c4f9d17",
        "source-review-adjudicator-key-unavailable",
        (
            "docker-build-infrastructure",
            "worker-claim-not-started",
            "l2-runtime-evidence-unavailable",
        ),
    ),
)


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _index(engine: AsyncEngine) -> tuple[bool, str]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT i.indisvalid, pg_get_expr(i.indpred, i.indrelid) "
                    "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = 'screening_attempts_infra_failed_idx'"
                )
            )
        ).one()
    return bool(row[0]), str(row[1])


def _names(predicate: str, code: str) -> bool:
    return f"'{code}'" in predicate


@pytest.mark.parametrize(("parent", "new_code", "kept"), _WIDENINGS)
async def test_infra_failed_index_round_trip(
    engine: AsyncEngine, parent: str, new_code: str, kept: tuple[str, ...]
) -> None:
    try:
        valid, predicate = await _index(engine)
        assert valid
        assert all(_names(predicate, code) for code in INFRA_AUTO_RETRY_REASON_CODES)

        _alembic("downgrade", parent)
        valid, predicate = await _index(engine)
        assert valid
        assert not _names(predicate, new_code)
        assert all(_names(predicate, code) for code in kept)

        _alembic("upgrade", "head")
        valid, predicate = await _index(engine)
        assert valid
        assert all(_names(predicate, code) for code in INFRA_AUTO_RETRY_REASON_CODES)
    finally:
        # Keep this worker database usable even when an assertion above fails.
        _alembic("upgrade", "head")
