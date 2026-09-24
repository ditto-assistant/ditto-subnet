"""Making the fee denomination explicit must not change any live price (#472).

Downgrades one step, writes pre-migration revisions exactly as production has
them, upgrades, and proves every fee, cooldown, revision, and timestamp is
unchanged while the denomination now records the fixed-TAO behaviour in force.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.db.queries.submission_settings import effective_submission_settings

_BEFORE = "b87d2e4f10a9"
_PAYMENT_ADDRESS = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_SNAPSHOT = text(
    "SELECT revision, parent_revision, cooldown_seconds, fee_amount_rao, reason, "
    "actor, created_at FROM submission_settings_revisions ORDER BY revision"
)


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def test_backfill_reproduces_the_current_fee_exactly(
    engine: AsyncEngine,
) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        _alembic("downgrade", _BEFORE)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO submission_settings_revisions (parent_revision, "
                    "cooldown_seconds, fee_amount_rao, reason, actor) "
                    "VALUES (1, 1800, 37271710, 'operator raised the fee', 'op')"
                )
            )
            before = (await connection.execute(_SNAPSHOT)).all()
        assert len(before) == 2

        _alembic("upgrade", "head")

        async with engine.begin() as connection:
            after = (await connection.execute(_SNAPSHOT)).all()
            denominations = set(
                await connection.scalars(
                    text(
                        "SELECT DISTINCT fee_denomination "
                        "FROM submission_settings_revisions"
                    )
                )
            )
            default = await connection.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'submission_settings_revisions' "
                    "AND column_name = 'fee_denomination'"
                )
            )
        assert after == before
        assert denominations == {"fixed_tao"}
        # No silent default: a future writer must name the denomination.
        assert default is None

        session: AsyncSession
        async with maker() as session:
            effective = await effective_submission_settings(
                session, default_payment_address=_PAYMENT_ADDRESS
            )
        latest = before[-1]
        assert (
            effective.revision,
            effective.cooldown_seconds,
            effective.fee_amount_rao,
        ) == (latest.revision, latest.cooldown_seconds, latest.fee_amount_rao)

        for statement in (
            "INSERT INTO submission_settings_revisions "
            "(parent_revision, cooldown_seconds, fee_amount_rao, reason, actor) "
            "VALUES (2, 1800, 40000000, 'writer forgot the mode', 'op')",
            "INSERT INTO submission_settings_revisions "
            "(parent_revision, cooldown_seconds, fee_amount_rao, fee_denomination, "
            "reason, actor) "
            "VALUES (2, 1800, 5, 'usd_indexed', 'five dollars target', 'op')",
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text(statement))
    finally:
        _alembic("upgrade", "head")
