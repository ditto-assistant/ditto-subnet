"""The infra-failure partial index follows the retried reason codes (#2444).

Upgrade must rebuild ``screening_attempts_infra_failed_idx`` over all three retried
codes; downgrade must restore the prior two-code predicate. Each direction leaves a
valid index and can run again from its own result.
"""

from __future__ import annotations

import os
import subprocess

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "e0f28816bca9"
_NEW_CODE = "l2-runtime-evidence-unavailable"


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


async def test_infra_failed_index_round_trip(engine: AsyncEngine) -> None:
    try:
        valid, predicate = await _index(engine)
        assert valid
        assert _NEW_CODE in predicate and "worker-claim-not-started" in predicate

        _alembic("downgrade", _PARENT)
        valid, predicate = await _index(engine)
        assert valid
        assert _NEW_CODE not in predicate
        assert "docker-build-infrastructure" in predicate
        assert "worker-claim-not-started" in predicate

        _alembic("upgrade", "head")
        valid, predicate = await _index(engine)
        assert valid
        assert _NEW_CODE in predicate and "worker-claim-not-started" in predicate
    finally:
        # Keep this worker database usable even when an assertion above fails.
        _alembic("upgrade", "head")
