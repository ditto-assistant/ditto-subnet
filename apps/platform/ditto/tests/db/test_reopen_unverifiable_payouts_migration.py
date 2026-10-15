"""Only receipt-less, unresolved ambiguity verdicts are reopened for rescan."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from ditto.db.models import SourceEmissionPayout

_OWNERSHIP = "unverifiable_payout: subnet ownership changed during distribution block"


def _migration():
    path = (
        Path(__file__).parents[3]
        / "alembic/versions/2026_10_15_reopen_unverifiable_source_payouts.py"
    )
    spec = importlib.util.spec_from_file_location("reopen_payouts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_reopen_targets_only_receiptless_unverifiable_rows(engine, session_maker):
    migration = _migration()
    now = datetime.now(UTC)
    rows = {
        "reopen": (True, _OWNERSHIP, {"receipt": None}),
        "has_receipt": (True, _OWNERSHIP, {"receipt": {"block": 2}}),
        "other_terminal": (True, "payout_has_vector_writes", {"receipt": None}),
        "pending": (False, "missing_or_duplicate_miner_payout", {"receipt": None}),
    }
    async with session_maker() as session, session.begin():
        for block, (key, (terminal, reason, proof)) in enumerate(rows.items(), 1):
            session.add(
                SourceEmissionPayout(
                    netuid=118,
                    block=block,
                    block_hash=key,
                    proof=proof,
                    created_at=now,
                    last_checked_at=now,
                    terminal=terminal,
                    blocked_reason=reason,
                )
            )

    def run(connection, step):
        with Operations.context(MigrationContext.configure(connection)):
            step()

    async def state():
        async with session_maker() as session:
            return {
                row.block_hash: (row.terminal, row.last_checked_at is None)
                for row in await session.scalars(select(SourceEmissionPayout))
            }

    async with engine.begin() as connection:
        await connection.run_sync(run, migration.upgrade)
    assert await state() == {
        "reopen": (False, True),
        "has_receipt": (True, False),
        "other_terminal": (True, False),
        "pending": (False, False),
    }
    async with engine.begin() as connection:
        await connection.run_sync(run, migration.downgrade)
    assert {key: terminal for key, (terminal, _) in (await state()).items()} == {
        "reopen": True,
        "has_receipt": True,
        "other_terminal": True,
        "pending": False,
    }
