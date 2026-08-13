"""The unreduced generation history the copy gate attributes holds with.

``list_eligible_ledger`` keeps one representative row per attested payment
owner. That is correct for ranking and wrong for copy attribution: the rows it
discards are exactly the ones that answer "who had this code first". red-dragon
v18 was held as a duplicate of an owner with two submissions total because
red-dragon's own v17 -- which had carried the shared module set for two days --
never survived owner reduction into the gate's view.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent, AgentStatus, EvaluationPayment, Score
from ditto.db.queries.scores import list_anti_copy_history, list_eligible_ledger

pytestmark = pytest.mark.asyncio

_BENCH = 8
_BASE = datetime(2026, 8, 9, 15, 49, 16, tzinfo=UTC)


async def _generation(
    session: AsyncSession,
    *,
    name: str,
    hotkey: str,
    coldkey: str | None,
    created_at: datetime,
    composites: tuple[float, ...] = (0.90, 0.91, 0.92),
    status: AgentStatus = AgentStatus.SCORED,
    bench_version: int = _BENCH,
    content_fingerprint: dict | None = None,
) -> UUID:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=hotkey,
            name=name,
            sha256=f"{agent_id.hex}{agent_id.hex}",
            size_bytes=524288,
            status=status,
            created_at=created_at,
            content_fingerprint=content_fingerprint,
            normalized_source_hash=f"nsh-{name}",
        )
    )
    await session.flush()
    if coldkey is not None:
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent_id.hex}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                amount_rao=1,
                dest_address="5Destination",
                timestamp=created_at,
            )
        )
    for index, composite in enumerate(composites):
        session.add(
            Score(
                agent_id=agent_id,
                validator_hotkey=f"validator-{index}",
                bench_version=bench_version,
                run_id=f"{name}-{index}",
                signature="ab" * 64,
                seed=42,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=500,
                n=114,
                generated_at=created_at + timedelta(minutes=index),
                created_at=created_at + timedelta(minutes=index),
                updated_at=created_at + timedelta(minutes=index),
            )
        )
    await session.flush()
    return agent_id


async def test_history_keeps_every_generation_the_ledger_reduces_away(
    session: AsyncSession,
) -> None:
    """The red-dragon shape: one owner, several generations, one ledger row."""
    v17 = await _generation(
        session,
        name="red-dragon-v17",
        hotkey="5DcpbvmTro",
        coldkey="5HgisASb3W",
        created_at=_BASE,
        composites=(0.92, 0.93, 0.94),
    )
    v18 = await _generation(
        session,
        name="red-dragon-v18",
        hotkey="5DcpbvmTro",
        coldkey="5HgisASb3W",
        created_at=_BASE + timedelta(days=1),
        composites=(0.94, 0.95, 0.96),
    )
    # Second hotkey on the same payment coldkey -- the kingbear pair.
    kingbear = await _generation(
        session,
        name="kingbear-mem-v1",
        hotkey="5KingbearHk",
        coldkey="5HgisASb3W",
        created_at=_BASE - timedelta(days=15),
        composites=(0.88, 0.89, 0.90),
    )

    ledger = await list_eligible_ledger(session, bench_version=_BENCH)
    history = await list_anti_copy_history(
        session, bench_version=_BENCH, before=_BASE + timedelta(days=2)
    )

    # One row survives owner reduction for the whole coldkey; all three are in
    # the history, which is the entire point of this query.
    assert len({row.agent_id for row in ledger}) == 1
    assert {row.agent_id for row in history} == {v17, v18, kingbear}


async def test_history_is_bounded_by_upload_chronology(
    session: AsyncSession,
) -> None:
    """Later rows are dead weight: every consumer compares upload order."""
    earlier = await _generation(
        session,
        name="earlier",
        hotkey="5Earlier",
        coldkey="5EarlierCold",
        created_at=_BASE,
    )
    await _generation(
        session,
        name="later",
        hotkey="5Later",
        coldkey="5LaterCold",
        created_at=_BASE + timedelta(hours=2),
    )

    history = await list_anti_copy_history(
        session, bench_version=_BENCH, before=_BASE + timedelta(hours=1)
    )
    assert [row.agent_id for row in history] == [earlier]


async def test_history_carries_the_moderation_columns(
    session: AsyncSession,
) -> None:
    """Fingerprints and payment provenance must survive, or nothing matches."""
    sketch = {"v": 2, "k": 256, "card": 3, "m": ["a", "b", "c"], "corpus": "kit-v2"}
    agent_id = await _generation(
        session,
        name="fingerprinted",
        hotkey="5Fingerprinted",
        coldkey="5FingerprintedCold",
        created_at=_BASE,
        content_fingerprint=sketch,
    )

    (row,) = await list_anti_copy_history(
        session, bench_version=_BENCH, before=_BASE + timedelta(hours=1)
    )
    assert row.agent_id == agent_id
    assert row.content_fingerprint == sketch
    assert row.normalized_source_hash == "nsh-fingerprinted"
    assert row.miner_coldkey == "5FingerprintedCold"
    assert row.first_seen.replace(tzinfo=UTC) == _BASE
    # Median of the three validator composites, same rule as the ledger.
    assert row.composite == pytest.approx(0.91)


async def test_history_excludes_unscored_and_other_eras(
    session: AsyncSession,
) -> None:
    """Held, banned and cross-version rows are not part of the pool.

    A held artifact is precisely one whose provenance is unresolved, so it must
    not silently become the named source of someone else's hold.
    """
    scored = await _generation(
        session,
        name="scored",
        hotkey="5Scored",
        coldkey="5ScoredCold",
        created_at=_BASE,
    )
    await _generation(
        session,
        name="held",
        hotkey="5Held",
        coldkey="5HeldCold",
        created_at=_BASE,
        status=AgentStatus.ATH_PENDING_REVIEW,
    )
    await _generation(
        session,
        name="other-era",
        hotkey="5OtherEra",
        coldkey="5OtherEraCold",
        created_at=_BASE,
        bench_version=_BENCH - 1,
    )

    history = await list_anti_copy_history(
        session, bench_version=_BENCH, before=_BASE + timedelta(hours=1)
    )
    assert [row.agent_id for row in history] == [scored]
