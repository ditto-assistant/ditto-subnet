"""Unit tests for :mod:`ditto.db.queries.audit` against SQLite-in-memory.

The append-only, hash-chained score audit log: entries link by SHA-256, replay
verifies, and any tampering (edited content or a broken link) is detectable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.queries.audit import (
    EVENT_FINALIZED,
    EVENT_SCORE,
    GENESIS_HASH,
    append_audit_entry,
    get_audit_head,
    list_audit_entries,
    verify_audit_chain,
)

_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_T0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)


async def _append_score(
    session: AsyncSession, *, composite: float = 0.8, seq_hint: int = 0
) -> None:
    async with session.begin():
        await append_audit_entry(
            session,
            agent_id=uuid4(),
            validator_hotkey=_VALIDATOR,
            event=EVENT_SCORE,
            payload={"run_id": f"run_{seq_hint}", "composite": composite, "seed": 42},
            recorded_at=_T0,
        )


class TestAppendAndChain:
    async def test_first_entry_links_to_genesis(self, session: AsyncSession) -> None:
        await _append_score(session, seq_hint=1)
        entries = await list_audit_entries(session)
        assert len(entries) == 1
        assert entries[0].prev_hash == GENESIS_HASH
        assert entries[0].seq >= 1
        assert entries[0].entry_hash != GENESIS_HASH

    async def test_chain_links_and_verifies(self, session: AsyncSession) -> None:
        for i in range(5):
            await _append_score(session, composite=0.5 + i * 0.05, seq_hint=i)
        entries = await list_audit_entries(session)
        assert len(entries) == 5
        # Each entry's prev_hash is the prior entry's entry_hash.
        for prev, cur in zip(entries, entries[1:], strict=False):
            assert cur.prev_hash == prev.entry_hash
        assert verify_audit_chain(entries) is True

    async def test_mixed_event_types_chain(self, session: AsyncSession) -> None:
        async with session.begin():
            await append_audit_entry(
                session,
                agent_id=uuid4(),
                validator_hotkey=_VALIDATOR,
                event=EVENT_SCORE,
                payload={"composite": 0.7},
                recorded_at=_T0,
            )
            await append_audit_entry(
                session,
                agent_id=uuid4(),
                validator_hotkey=None,
                event=EVENT_FINALIZED,
                payload={"median_composite": 0.7, "quorum": 3},
                recorded_at=_T0,
            )
        entries = await list_audit_entries(session)
        assert [e.event for e in entries] == [EVENT_SCORE, EVENT_FINALIZED]
        assert entries[1].validator_hotkey is None
        assert verify_audit_chain(entries) is True

    async def test_head_none_when_empty(self, session: AsyncSession) -> None:
        assert await get_audit_head(session) is None

    async def test_head_tracks_latest(self, session: AsyncSession) -> None:
        await _append_score(session, seq_hint=1)
        await _append_score(session, seq_hint=2)
        head = await get_audit_head(session)
        entries = await list_audit_entries(session)
        assert head is not None
        assert head.seq == entries[-1].seq
        assert head.entry_hash == entries[-1].entry_hash


class TestTamperDetection:
    async def test_edited_payload_breaks_verification(
        self, session: AsyncSession
    ) -> None:
        for i in range(3):
            await _append_score(session, seq_hint=i)
        entries = await list_audit_entries(session)
        # Tamper: rewrite a historical entry's payload without re-hashing.
        entries[1].payload = {"run_id": "run_1", "composite": 0.999, "seed": 42}
        assert verify_audit_chain(entries) is False

    async def test_broken_link_breaks_verification(self, session: AsyncSession) -> None:
        for i in range(3):
            await _append_score(session, seq_hint=i)
        entries = await list_audit_entries(session)
        # Tamper: drop the middle entry, so seq 3's prev_hash no longer links.
        assert verify_audit_chain([entries[0], entries[2]]) is False

    async def test_wrong_genesis_breaks_verification(
        self, session: AsyncSession
    ) -> None:
        await _append_score(session, seq_hint=1)
        entries = await list_audit_entries(session)
        assert verify_audit_chain(entries, expected_prev="ff" * 32) is False

    async def test_midchain_page_verifies_from_prior_hash(
        self, session: AsyncSession
    ) -> None:
        for i in range(4):
            await _append_score(session, seq_hint=i)
        entries = await list_audit_entries(session)
        # A page starting at the 3rd entry verifies when seeded with the 2nd's hash.
        page = entries[2:]
        assert verify_audit_chain(page, expected_prev=entries[1].entry_hash) is True
        assert verify_audit_chain(page) is False  # ...but not from genesis


class TestPagination:
    async def test_since_seq_pages_forward(self, session: AsyncSession) -> None:
        for i in range(5):
            await _append_score(session, seq_hint=i)
        first = await list_audit_entries(session, since_seq=0, limit=2)
        assert len(first) == 2
        nxt = await list_audit_entries(session, since_seq=first[-1].seq, limit=2)
        assert len(nxt) == 2
        assert nxt[0].seq > first[-1].seq

    async def test_limit_caps_page(self, session: AsyncSession) -> None:
        for i in range(4):
            await _append_score(session, seq_hint=i)
        assert len(await list_audit_entries(session, limit=3)) == 3
