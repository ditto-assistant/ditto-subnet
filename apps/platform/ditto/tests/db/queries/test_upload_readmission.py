"""Classifying uploads that readmit the owner's own lineage (SN-485 section 1).

Reuses the same-owner duplicate machinery: byte-identical bytes re-run on
purpose are an identical rescore; the same normalized source under new bytes
is a near-duplicate. Only the owner's uploads are classified, never score
re-tests, so platform-caused re-tests stay unlabelled and free.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import EvaluationPayment, Score, UploadReadmission
from ditto.db.queries.agents import insert_agent
from ditto.db.queries.audit import EVENT_SCORE_RETEST_QUEUED, append_audit_entry
from ditto.db.queries.readmission import (
    classify_upload_readmission,
    record_upload_readmission,
)
from ditto.db.queries.score_retests import DUELING_ROTATION_RETEST_BASIS

pytestmark = pytest.mark.asyncio

COLDKEY = "5CKLineageOwner"


async def _paid_agent(
    session: AsyncSession,
    *,
    coldkey: str = COLDKEY,
    sha256: str | None = None,
    normalized: str | None = None,
    scores: int = 0,
) -> UUID:
    agent_id = uuid4()
    async with session.begin():
        await insert_agent(
            session,
            agent_id=agent_id,
            miner_hotkey="5HKLineage",
            name=f"lineage-{agent_id.hex[:8]}",
            sha256=sha256 or (uuid4().hex + uuid4().hex),
            size_bytes=10,
            normalized_source_hash=normalized,
        )
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent_id.int:064x}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey="5HKLineage",
                miner_coldkey=coldkey,
                amount_rao=75_000_000,
                dest_address="5Dest",
                timestamp=datetime.now(UTC) - timedelta(days=2),
            )
        )
        for index in range(scores):
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=18,
                    validator_hotkey=f"5V-{index}",
                    run_id=f"{agent_id}-{index}",
                    signature=None,
                    seed=index,
                    composite=0.5,
                    tool_mean=0.5,
                    memory_mean=0.5,
                    median_ms=1,
                    n=10,
                    details={},
                    generated_at=datetime.now(UTC),
                )
            )
    return agent_id


async def test_an_explicit_identical_rescore_is_a_readmission(
    session: AsyncSession,
) -> None:
    lineage = await _paid_agent(session, scores=3)
    label = await classify_upload_readmission(
        session,
        miner_coldkey=COLDKEY,
        sha256="ab" * 32,
        normalized_source_hash=None,
        identical_agent_id=lineage,
    )
    assert label is not None
    assert (label.kind, label.lineage_agent_id) == ("identical_rescore", lineage)
    assert label.lineage_evaluation_completed is True


async def test_the_same_normalized_source_under_new_bytes_is_a_near_duplicate(
    session: AsyncSession,
) -> None:
    lineage = await _paid_agent(session, normalized="n" * 64, scores=2)
    label = await classify_upload_readmission(
        session,
        miner_coldkey=COLDKEY,
        sha256="cd" * 32,
        normalized_source_hash="n" * 64,
        identical_agent_id=None,
    )
    assert label is not None
    assert (label.kind, label.lineage_agent_id) == ("near_duplicate", lineage)
    # Two of three scores: its evaluation never completed.
    assert label.lineage_evaluation_completed is False


async def test_another_owners_source_is_never_this_owners_lineage(
    session: AsyncSession,
) -> None:
    await _paid_agent(session, coldkey="5CKSomeoneElse", normalized="m" * 64)
    assert (
        await classify_upload_readmission(
            session,
            miner_coldkey=COLDKEY,
            sha256="ef" * 32,
            normalized_source_hash="m" * 64,
            identical_agent_id=None,
        )
        is None
    )


async def test_a_dueling_rotation_retest_never_creates_a_readmission(
    session: AsyncSession,
) -> None:
    """SN-486: platform-caused re-tests are score re-tests, not uploads."""
    lineage = await _paid_agent(session, normalized="r" * 64, scores=3)
    async with session.begin():
        await append_audit_entry(
            session,
            agent_id=lineage,
            validator_hotkey="5V-0",
            event=EVENT_SCORE_RETEST_QUEUED,
            payload={
                "request_id": str(uuid4()),
                "basis": DUELING_ROTATION_RETEST_BASIS,
                "bench_version": 18,
                "catch_up": True,
                "catch_up_source": "ath_clear",
                "automatic": True,
            },
            recorded_at=datetime.now(UTC),
        )
    # The re-test is not an upload, so nothing labels it, and the lineage's
    # own label is unaffected by it.
    assert await session.scalar(select(UploadReadmission)) is None
    await session.commit()
    rerun = await _paid_agent(session, normalized="r" * 64)
    label = await classify_upload_readmission(
        session,
        miner_coldkey=COLDKEY,
        sha256="12" * 32,
        normalized_source_hash="r" * 64,
        identical_agent_id=None,
    )
    assert label is not None and label.lineage_agent_id == lineage
    await session.commit()
    async with session.begin():
        await record_upload_readmission(
            session,
            agent_id=rerun,
            label=label,
            bench_version=19,
            miner_coldkey=COLDKEY,
        )
    rows = list(await session.scalars(select(UploadReadmission)))
    assert [(row.agent_id, row.kind) for row in rows] == [(rerun, "near_duplicate")]
