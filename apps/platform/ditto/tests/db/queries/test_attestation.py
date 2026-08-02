"""Tests for :mod:`ditto.db.queries.attestation`.

Includes the load-bearing ownership tests: a mutually signed owner link gives
the linked hotkeys one emission position while preserving their rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, EvaluationPayment
from ditto.db.queries.attestation import (
    AttestationReplayedError,
    get_bound_coldkey_for_hotkey,
    list_attestations_for_hotkey,
    list_linked_hotkeys,
    record_attestation,
    revoke_attestation,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.scores import (
    MIN_ELIGIBLE_CASES,
    list_eligible_ledger,
    upsert_score,
)

_NETUID = 118
_A = "5AAAaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_B = "5BBBeW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_C = "5CCCvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_VALIDATOR = "5CiPPseXPECbkjWCa6MnjNokrgYjMqmKndv2rSnekmSK2DjL"
_ISSUED = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


async def _attest(
    session: AsyncSession,
    *,
    a: str,
    b: str,
    netuid: int = _NETUID,
    lo_kind: str = "hotkey",
    hi_kind: str = "hotkey",
):
    """Record a link. Signature verification is covered separately; these tests
    exercise the relational invariants."""
    lo, hi = (a, b) if a <= b else (b, a)
    async with session.begin():
        return await record_attestation(
            session,
            netuid=netuid,
            hotkey_lo=lo,
            hotkey_hi=hi,
            nonce=uuid4(),
            issued_at=_ISSUED,
            lo_key_kind=lo_kind,
            lo_signer=lo,
            lo_signature="ab" * 64,
            hi_key_kind=hi_kind,
            hi_signer=hi,
            hi_signature="cd" * 64,
        )


class TestRecordAttestation:
    async def test_records_and_resolves_symmetrically(
        self, session: AsyncSession
    ) -> None:
        """The link reads the same from either end."""
        await _attest(session, a=_A, b=_B)
        from_a = await list_linked_hotkeys(session, hotkey=_A, netuid=_NETUID)
        from_b = await list_linked_hotkeys(session, hotkey=_B, netuid=_NETUID)
        assert [link.hotkey for link in from_a] == [_B]
        assert [link.hotkey for link in from_b] == [_A]

    async def test_replayed_nonce_is_rejected(self, session: AsyncSession) -> None:
        """The replay guard: a captured attestation cannot be submitted twice."""
        nonce = uuid4()
        lo, hi = (_A, _B) if _A <= _B else (_B, _A)
        async with session.begin():
            await record_attestation(
                session,
                netuid=_NETUID,
                hotkey_lo=lo,
                hotkey_hi=hi,
                nonce=nonce,
                issued_at=_ISSUED,
                lo_key_kind="hotkey",
                lo_signer=lo,
                lo_signature="ab" * 64,
                hi_key_kind="hotkey",
                hi_signer=hi,
                hi_signature="cd" * 64,
            )
        lo2, hi2 = (_A, _C) if _A <= _C else (_C, _A)
        with pytest.raises(AttestationReplayedError, match="nonce"):
            async with session.begin():
                await record_attestation(
                    session,
                    netuid=_NETUID,
                    hotkey_lo=lo2,
                    hotkey_hi=hi2,
                    nonce=nonce,  # same nonce, different pair
                    issued_at=_ISSUED,
                    lo_key_kind="hotkey",
                    lo_signer=lo2,
                    lo_signature="ab" * 64,
                    hi_key_kind="hotkey",
                    hi_signer=hi2,
                    hi_signature="cd" * 64,
                )

    async def test_duplicate_active_pair_is_rejected(
        self, session: AsyncSession
    ) -> None:
        await _attest(session, a=_A, b=_B)
        with pytest.raises(AttestationReplayedError, match="already links"):
            await _attest(session, a=_A, b=_B)

    async def test_reversed_pair_is_the_same_link(self, session: AsyncSession) -> None:
        """Canonical ordering means a pair cannot be linked twice by swapping."""
        await _attest(session, a=_A, b=_B)
        with pytest.raises(AttestationReplayedError, match="already links"):
            await _attest(session, a=_B, b=_A)

    async def test_other_netuid_is_invisible(self, session: AsyncSession) -> None:
        await _attest(session, a=_A, b=_B, netuid=64)
        assert await list_linked_hotkeys(session, hotkey=_A, netuid=_NETUID) == []

    async def test_grade_is_recorded(self, session: AsyncSession) -> None:
        await _attest(session, a=_A, b=_B, lo_kind="coldkey", hi_kind="coldkey")
        links = await list_linked_hotkeys(session, hotkey=_A, netuid=_NETUID)
        assert links[0].grade == "coldkey-coldkey"


class TestNotTransitive:
    async def test_links_do_not_chain(self, session: AsyncSession) -> None:
        """A--B and B--C must not link A and C.

        Symmetric edges plus transitivity would let B bridge two owners who
        never signed anything with each other.
        """
        await _attest(session, a=_A, b=_B)
        await _attest(session, a=_B, b=_C)
        from_a = await list_linked_hotkeys(session, hotkey=_A, netuid=_NETUID)
        assert [link.hotkey for link in from_a] == [_B]
        assert _C not in {link.hotkey for link in from_a}

    async def test_multiple_direct_links_all_resolve(
        self, session: AsyncSession
    ) -> None:
        """A miner who rotated twice attests each pair they need."""
        await _attest(session, a=_A, b=_B)
        await _attest(session, a=_A, b=_C)
        from_a = await list_linked_hotkeys(session, hotkey=_A, netuid=_NETUID)
        assert sorted(link.hotkey for link in from_a) == sorted([_B, _C])


class TestRevocation:
    async def test_revoked_link_stops_resolving(self, session: AsyncSession) -> None:
        row = await _attest(session, a=_A, b=_B)
        async with session.begin():
            await revoke_attestation(
                session,
                attestation_id=row.attestation_id,
                revoked_by=_A,
                reason="key sold",
            )
        assert await list_linked_hotkeys(session, hotkey=_A, netuid=_NETUID) == []
        assert await list_linked_hotkeys(session, hotkey=_B, netuid=_NETUID) == []

    async def test_revoked_row_is_retained_for_audit(
        self, session: AsyncSession
    ) -> None:
        """ "Was the link live when that submission was screened" is the
        question a dispute turns on, so the row is never deleted."""
        row = await _attest(session, a=_A, b=_B)
        async with session.begin():
            await revoke_attestation(
                session,
                attestation_id=row.attestation_id,
                revoked_by=_A,
                reason="key sold",
            )
        rows = await list_attestations_for_hotkey(session, hotkey=_B, netuid=_NETUID)
        assert len(rows) == 1
        assert rows[0].revoked_at is not None
        assert rows[0].revoked_reason == "key sold"

    async def test_revoking_twice_is_a_noop(self, session: AsyncSession) -> None:
        row = await _attest(session, a=_A, b=_B)
        async with session.begin():
            await revoke_attestation(
                session,
                attestation_id=row.attestation_id,
                revoked_by=_A,
                reason="first",
            )
        async with session.begin():
            again = await revoke_attestation(
                session,
                attestation_id=row.attestation_id,
                revoked_by=_A,
                reason="second",
            )
        assert again is None

    async def test_link_can_be_reestablished_after_revocation(
        self, session: AsyncSession
    ) -> None:
        """The partial unique index constrains only *active* links."""
        row = await _attest(session, a=_A, b=_B)
        async with session.begin():
            await revoke_attestation(
                session,
                attestation_id=row.attestation_id,
                revoked_by=_A,
                reason="mistake",
            )
        await _attest(session, a=_A, b=_B)
        links = await list_linked_hotkeys(session, hotkey=_A, netuid=_NETUID)
        assert [link.hotkey for link in links] == [_B]


async def _seed_scored_agent(
    session: AsyncSession,
    *,
    miner: str,
    coldkey: str,
    composite: float,
    created_at: datetime,
) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=miner,
        name=f"agent-{miner[-4:]}",
        sha256=uuid4().hex + uuid4().hex,
        size_bytes=524288,
        status=AgentStatus.SCORED,
        created_at=created_at,
    )
    async with session.begin():
        session.add(agent)
        await session.flush()
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent.agent_id.hex}",
                extrinsic_index=0,
                agent_id=agent.agent_id,
                miner_hotkey=miner,
                miner_coldkey=coldkey,
                amount_rao=1,
                dest_address="5Destination",
                timestamp=created_at,
            )
        )
        await upsert_score(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=_VALIDATOR,
            run_id="run_1",
            seed=42,
            composite=composite,
            tool_mean=composite,
            memory_mean=composite,
            median_ms=500,
            n=MIN_ELIGIBLE_CASES,
            generated_at=created_at,
            bench_version=MIN_SCOREABLE_BENCH_VERSION,
        )
    return agent


class TestBoundColdkey:
    async def test_returns_most_recent_payment_coldkey(
        self, session: AsyncSession
    ) -> None:
        """Most-recent, not any-ever: a hotkey that changed hands must not keep
        authorising its previous owner to sign for it."""
        base = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
        await _seed_scored_agent(
            session, miner=_A, coldkey="5OldOwner", composite=0.9, created_at=base
        )
        await _seed_scored_agent(
            session,
            miner=_A,
            coldkey="5NewOwner",
            composite=0.8,
            created_at=base + timedelta(days=1),
        )
        assert await get_bound_coldkey_for_hotkey(session, hotkey=_A) == "5NewOwner"

    async def test_unknown_hotkey_has_no_binding(self, session: AsyncSession) -> None:
        assert await get_bound_coldkey_for_hotkey(session, hotkey=_C) is None


class TestEmissionSlotsFollowProvenOwnership:
    async def test_attestation_collapses_distinct_coldkeys_to_one_slot(
        self, session: AsyncSession
    ) -> None:
        """Two proved hotkeys belonging to one operator get one best slot."""
        first_seen = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
        await _seed_scored_agent(
            session,
            miner=_A,
            coldkey="5ColdkeyA",
            composite=0.90,
            created_at=first_seen,
        )
        await _seed_scored_agent(
            session,
            miner=_B,
            coldkey="5ColdkeyB",
            composite=0.85,
            created_at=first_seen + timedelta(hours=1),
        )

        before = await list_eligible_ledger(session)
        assert len(before) == 2
        # Close the transaction the read autobegan so the write can open its own.
        await session.rollback()

        await _attest(session, a=_A, b=_B)

        after = await list_eligible_ledger(session)
        assert len(after) == 1
        assert after[0].miner_hotkey == _A
        assert after[0].composite == 0.90

    async def test_attestation_does_not_split_one_coldkey_into_two_slots(
        self, session: AsyncSession
    ) -> None:
        """The abuse shape the constraint names: claiming more slots.

        One coldkey funding two hotkeys holds exactly one emission position. An
        attestation between those two hotkeys must not turn that into two.
        """
        first_seen = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
        await _seed_scored_agent(
            session,
            miner=_A,
            coldkey="5SharedColdkey",
            composite=0.90,
            created_at=first_seen,
        )
        await _seed_scored_agent(
            session,
            miner=_B,
            coldkey="5SharedColdkey",
            composite=0.85,
            created_at=first_seen + timedelta(hours=1),
        )

        await _attest(session, a=_A, b=_B)

        ledger = await list_eligible_ledger(session)
        assert len(ledger) == 1
        assert ledger[0].miner_coldkey == "5SharedColdkey"

    async def test_revocation_restores_independent_emission_slots(
        self, session: AsyncSession
    ) -> None:
        """A revoked proof stops collapsing the owners on the next read."""
        first_seen = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
        await _seed_scored_agent(
            session,
            miner=_A,
            coldkey="5ColdkeyA",
            composite=0.90,
            created_at=first_seen,
        )
        await _seed_scored_agent(
            session,
            miner=_B,
            coldkey="5ColdkeyB",
            composite=0.85,
            created_at=first_seen + timedelta(hours=1),
        )
        link = await _attest(session, a=_A, b=_B)
        attestation_id = link.attestation_id
        assert len(await list_eligible_ledger(session)) == 1
        await session.rollback()

        async with session.begin():
            revoked = await revoke_attestation(
                session,
                attestation_id=attestation_id,
                revoked_by=_A,
                reason="owners separated",
            )
        assert revoked is not None
        assert len(await list_eligible_ledger(session)) == 2
