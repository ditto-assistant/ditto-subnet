"""Real-Postgres migration/backfill and resolution concurrency proof."""

import asyncio
import os
import subprocess
from typing import Literal
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_models.admin_copy_review import AdminCopyReviewResolveRequest
from ditto.api_server.endpoints.admin_copy_review import resolve_copy_review
from ditto.db import create_db_engine
from ditto.db.models import Agent, AgentStatus, AthReview
from ditto.tests.pgharness import truncate_test_tables

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


# Reset rows left by earlier tests before exercising guarded downgrades.
async def test_conflicting_concurrent_resolution_has_one_winner() -> None:
    _alembic("upgrade", "head")
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    original_id = UUID("00000000-0000-0000-0000-000000000201")
    held_id = UUID("00000000-0000-0000-0000-000000000202")
    async with maker() as session, session.begin():
        await truncate_test_tables(session, "agents")
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
