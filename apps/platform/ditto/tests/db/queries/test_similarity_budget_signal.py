"""The similarity signal reaches the queue path intact.

These cover the *read*, not the decision: that the deferred ``anticopy``
fingerprint can be projected for a bounded id set without loading ``Agent``
entities, that a submission with no usable evidence is reported as such rather
than silently treated as a match, and that the live-lease set the gate compares
against is the same ``ISSUED``-and-unexpired set the owner rail already uses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import Agent, ValidatorTicket
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.similarity_budget import (
    live_lease_agent_ids,
    live_lease_sketches,
    load_submission_sketches,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
_TTL = timedelta(minutes=90)
_BENCH = MIN_SCOREABLE_BENCH_VERSION


def _sketch(*hashes: str, card: int | None = None) -> dict:
    """A fingerprint in the exact shape ``compute_content_fingerprint`` emits."""
    return {
        "v": 2,
        "k": 256,
        "card": card if card is not None else len(hashes),
        "m": list(hashes),
        "corpus": "c0ffee",
    }


async def _seed(
    session: AsyncSession,
    *,
    fingerprint: dict | None,
    lease_validator: str | None = None,
    lease_status: TicketStatus = TicketStatus.ISSUED,
    lease_deadline: datetime | None = None,
    lease_purpose: TicketPurpose = TicketPurpose.CANONICAL_QUORUM,
    miner_hotkey: str | None = None,
    created_at: datetime | None = None,
) -> UUID:
    agent_id = uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=miner_hotkey or f"5Hotkey{agent_id.hex[:8]}",
                name=f"agent-{agent_id.hex[:6]}",
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                content_fingerprint=fingerprint,
                created_at=created_at or (_NOW - timedelta(hours=1)),
            )
        )
        await session.flush()
        if lease_validator is not None:
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH,
                    validator_hotkey=lease_validator,
                    slot_id="slot-0",
                    status=lease_status,
                    purpose=lease_purpose,
                    purpose_revision=1,
                    issued_at=_NOW - timedelta(minutes=10),
                    deadline=lease_deadline or (_NOW + _TTL),
                    attempt_count=1,
                )
            )
    return agent_id


async def test_projects_the_deferred_fingerprint_without_loading_entities(
    session: AsyncSession,
) -> None:
    """The sketch arrives as a plain dict, byte-for-byte as it was stored."""
    stored = _sketch("00ff", "01ff", card=900)
    agent_id = await _seed(session, fingerprint=stored)

    sketches = await load_submission_sketches(session, agent_ids=[agent_id])

    assert sketches[agent_id].fingerprint == stored
    assert sketches[agent_id].comparable is True
    # The deferred group must not have been dragged into the identity map.
    assert not session.identity_map.values()


async def test_missing_and_empty_evidence_is_reported_not_guessed(
    session: AsyncSession,
) -> None:
    """No fingerprint, and an empty one, both mean "never group this"."""
    absent = await _seed(session, fingerprint=None)
    empty = await _seed(session, fingerprint=_sketch(card=0))
    unknown = uuid4()

    sketches = await load_submission_sketches(
        session, agent_ids=[absent, empty, unknown]
    )

    assert sketches[absent].comparable is False
    assert sketches[empty].comparable is False
    assert unknown not in sketches


async def test_empty_request_does_not_query(session: AsyncSession) -> None:
    assert await load_submission_sketches(session, agent_ids=[]) == {}


async def test_live_leases_are_issued_and_unexpired_only(
    session: AsyncSession,
) -> None:
    """Same liveness rule as the owner rail: ``ISSUED`` and deadline ahead."""
    live = await _seed(
        session, fingerprint=_sketch("aa"), lease_validator="5ValidatorA"
    )
    overdue = await _seed(
        session,
        fingerprint=_sketch("bb"),
        lease_validator="5ValidatorB",
        lease_deadline=_NOW - timedelta(minutes=1),
    )
    finished = await _seed(
        session,
        fingerprint=_sketch("cc"),
        lease_validator="5ValidatorC",
        lease_status=TicketStatus.SCORED,
    )
    waiting = await _seed(session, fingerprint=_sketch("dd"))

    assert await live_lease_agent_ids(session, now=_NOW) == {live}
    assert overdue not in await live_lease_agent_ids(session, now=_NOW)
    assert finished not in await live_lease_agent_ids(session, now=_NOW)
    assert waiting not in await live_lease_agent_ids(session, now=_NOW)


async def test_continual_retest_leases_do_not_occupy_the_similarity_budget(
    session: AsyncSession,
) -> None:
    """Spare-capacity rescoring of an older twin must not serialize a newer one."""
    canonical = await _seed(
        session, fingerprint=_sketch("aa"), lease_validator="5ValidatorA"
    )
    retest = await _seed(
        session,
        fingerprint=_sketch("bb"),
        lease_validator="5ValidatorB",
        lease_purpose=TicketPurpose.CONTINUAL_RETEST,
    )

    live = await live_lease_agent_ids(session, now=_NOW)
    assert live == {canonical}
    assert retest not in live


async def test_live_lease_sketches_exclude_the_candidate_itself(
    session: AsyncSession,
) -> None:
    """A candidate already holding a lease is not its own near-twin.

    It can legitimately hold one from another validator while a second
    validator considers it, and counting it against itself would make the gate
    refuse to fill a submission's own remaining quorum slots.
    """
    candidate = await _seed(
        session, fingerprint=_sketch("aa"), lease_validator="5ValidatorA"
    )
    other = await _seed(
        session, fingerprint=_sketch("bb"), lease_validator="5ValidatorB"
    )

    sketches = await live_lease_sketches(session, now=_NOW, exclude=[candidate])

    assert [s.agent_id for s in sketches] == [other]


async def test_miner_has_newer_canonical_work_when_a_later_version_is_waiting(
    session: AsyncSession,
) -> None:
    from ditto.db.queries.queue_order import miner_has_newer_canonical_work

    hotkey = "5SameMinerHotkey00000000000000000000000000000"
    older = await _seed(
        session,
        fingerprint=_sketch("aa"),
        miner_hotkey=hotkey,
        created_at=_NOW - timedelta(hours=2),
    )
    await _seed(
        session,
        fingerprint=_sketch("bb"),
        miner_hotkey=hotkey,
        created_at=_NOW - timedelta(minutes=5),
    )

    assert (
        await miner_has_newer_canonical_work(
            session,
            miner_hotkey=hotkey,
            created_before=_NOW - timedelta(hours=2),
            bench_version=_BENCH,
        )
        is True
    )
    older_row = await session.get(Agent, older)
    assert older_row is not None
    assert (
        await miner_has_newer_canonical_work(
            session,
            miner_hotkey=hotkey,
            created_before=older_row.created_at,
            bench_version=_BENCH,
        )
        is True
    )
    assert (
        await miner_has_newer_canonical_work(
            session,
            miner_hotkey=hotkey,
            created_before=_NOW,
            bench_version=_BENCH,
        )
        is False
    )
