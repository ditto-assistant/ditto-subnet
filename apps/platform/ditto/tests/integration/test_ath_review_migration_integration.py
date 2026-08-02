"""Real-Postgres migration/backfill and resolution concurrency proof."""

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_models.admin_copy_review import AdminCopyReviewResolveRequest
from ditto.api_server.endpoints.admin_copy_review import resolve_copy_review
from ditto.db import create_db_engine
from ditto.db.models import Agent, AgentStatus, AthReview

pytestmark = pytest.mark.integration

_AUDIT = UUID("00000000-0000-0000-0000-000000000101")
_SCORE = UUID("00000000-0000-0000-0000-000000000102")
_CREATED = UUID("00000000-0000-0000-0000-000000000103")


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
    )


async def test_upgrade_backfills_timestamp_provenance_and_is_idempotent() -> None:
    _alembic("downgrade", "base")
    _alembic("upgrade", "c53fa6d2b194")
    engine = create_db_engine()
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE agents CASCADE"))
        for agent_id, created in (
            (_AUDIT, datetime(2026, 7, 16, 10, tzinfo=UTC)),
            (_SCORE, datetime(2026, 7, 16, 10, 10, tzinfo=UTC)),
            (_CREATED, datetime(2026, 7, 16, 10, 20, tzinfo=UTC)),
        ):
            await conn.execute(
                text(
                    "INSERT INTO agents "
                    "(agent_id, miner_hotkey, name, sha256, status, created_at) "
                    "VALUES (:id, :miner, :name, :sha, 'ath_pending_review', :created)"
                ),
                {
                    "id": agent_id,
                    "miner": f"5{agent_id.hex[-8:]}",
                    "name": str(agent_id),
                    "sha": agent_id.hex * 2,
                    "created": created,
                },
            )
        await conn.execute(
            text(
                "INSERT INTO score_audit_log "
                "(agent_id, event, payload, prev_hash, entry_hash, recorded_at) "
                "VALUES (:id, 'agent_finalized', '{}'::jsonb, :prev, :entry, :at)"
            ),
            {
                "id": _AUDIT,
                "prev": "0" * 64,
                "entry": "1" * 64,
                "at": datetime(2026, 7, 16, 11, tzinfo=UTC),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO scores "
                "(agent_id, validator_hotkey, run_id, seed, composite, tool_mean, "
                "memory_mean, median_ms, n, generated_at) VALUES "
                "(:id, '5Validator', 'run', 1, .8, .8, .8, 100, 10, :at)"
            ),
            {"id": _SCORE, "at": datetime(2026, 7, 16, 11, 10, tzinfo=UTC)},
        )
    await engine.dispose()

    _alembic("upgrade", "head")
    _alembic("upgrade", "head")
    engine = create_db_engine()
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT agent_id, opened_at, algorithm_provenance "
                        "FROM ath_reviews ORDER BY agent_id"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 3
    by_id = {row["agent_id"]: row for row in rows}
    assert by_id[_AUDIT]["opened_at"] == datetime(2026, 7, 16, 11, tzinfo=UTC)
    assert by_id[_SCORE]["opened_at"] == datetime(2026, 7, 16, 11, 10, tzinfo=UTC)
    assert by_id[_CREATED]["opened_at"] == datetime(2026, 7, 16, 10, 20, tzinfo=UTC)
    assert (
        by_id[_AUDIT]["algorithm_provenance"]["opened_at_source"]
        == "agent_finalized_audit"
    )
    assert by_id[_SCORE]["algorithm_provenance"]["opened_at_source"] == "latest_score"
    assert (
        by_id[_CREATED]["algorithm_provenance"]["opened_at_source"]
        == "agent_created_at_fallback"
    )
    assert all(
        row["algorithm_provenance"]["snapshot_order"]
        == "before-fingerprint-metadata-backfill"
        for row in rows
    )
    async with engine.connect() as conn:
        transaction = await conn.begin()
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "UPDATE ath_reviews SET status = 'resolved' "
                    "WHERE agent_id = :agent_id"
                ),
                {"agent_id": _AUDIT},
            )
        await transaction.rollback()
    await engine.dispose()


async def test_conflicting_concurrent_resolution_has_one_winner() -> None:
    _alembic("upgrade", "head")
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    original_id = UUID("00000000-0000-0000-0000-000000000201")
    held_id = UUID("00000000-0000-0000-0000-000000000202")
    async with maker() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE agents CASCADE"))
        session.add_all(
            [
                Agent(
                    agent_id=original_id,
                    miner_hotkey="5Original",
                    name="original",
                    sha256=original_id.hex * 2,
                    status=AgentStatus.SCORED,
                ),
                Agent(
                    agent_id=held_id,
                    miner_hotkey="5Held",
                    name="held",
                    sha256=held_id.hex * 2,
                    status=AgentStatus.ATH_PENDING_REVIEW,
                    duplicate_of=original_id,
                    review_reason="matched immutable evidence",
                ),
                AthReview(
                    review_id=held_id,
                    agent_id=held_id,
                    status="pending",
                    original_duplicate_of=original_id,
                    original_reason="matched immutable evidence",
                    original_policy_version=8,
                    original_evidence={},
                    algorithm_provenance={"reference_provenance": "legacy"},
                ),
            ]
        )

    async def resolve(action: Literal["clear", "reject"]) -> object:
        async with maker() as session:
            try:
                return await resolve_copy_review(
                    held_id,
                    AdminCopyReviewResolveRequest(
                        resolution=action, reason=f"Operator chose {action}"
                    ),
                    None,
                    session,
                    "operator",
                )
            except HTTPException as exc:
                return exc

    outcomes = await asyncio.gather(resolve("clear"), resolve("reject"))
    assert sorted(
        outcome.status_code if isinstance(outcome, HTTPException) else 200
        for outcome in outcomes
    ) == [200, 409]
    async with maker() as session:
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == held_id)
        )
        assert review is not None and review.status == "resolved"
        assert review.resolution in {"clear", "reject"}
    await engine.dispose()


async def test_operator_reason_constraints_have_no_upper_bound() -> None:
    _alembic("upgrade", "head")
    engine = create_db_engine()
    async with engine.connect() as conn:
        bounded = (
            (
                await conn.execute(
                    text(
                        "SELECT conrelid::regclass::text, conname, "
                        "pg_get_constraintdef(oid) "
                        "FROM pg_constraint "
                        "WHERE contype = 'c' "
                        "AND pg_get_constraintdef(oid) ~* 'reason' "
                        "AND pg_get_constraintdef(oid) ~ '500' "
                        "ORDER BY 1, 2"
                    )
                )
            )
            .tuples()
            .all()
        )
    await engine.dispose()
    assert bounded == []
