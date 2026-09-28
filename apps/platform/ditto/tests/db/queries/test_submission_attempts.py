"""Owner provenance, chronology, paging, and reservation capacity in Postgres."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.submission_attempts import AttemptControlSettings
from ditto.db.models import (
    OwnerAttestation,
    Score,
    SubmissionAttemptSettingsRevision,
    ValidatorTicket,
)
from ditto.db.queries.submission_attempts import (
    attempt_settings,
    compare_attempt,
    lock_attempt_owner,
)
from ditto.db.queries.submission_settings import (
    EffectiveSubmissionSettings,
    reserve_upload_admission,
)
from ditto.tests.submission_attempt_fixtures import NOW, paid_attempt, profile

pytestmark = pytest.mark.asyncio


async def _compare(session, *, hotkey="hotkey-a", coldkey="owner-a", **kwargs):
    return await compare_attempt(
        session,
        profile=profile(),
        hotkey=hotkey,
        coldkey=coldkey,
        netuid=118,
        settings=AttemptControlSettings(mode="enforce"),
        revision=0,
        now=NOW,
        **kwargs,
    )


async def _link(session, a, b, *, revoked_at=None, created_at=None, coldkey_proof=True):
    lo, hi = sorted([a, b])
    session.add(
        OwnerAttestation(
            netuid=118,
            hotkey_lo=lo,
            hotkey_hi=hi,
            nonce=uuid4(),
            issued_at=NOW - timedelta(hours=1),
            created_at=created_at or (NOW - timedelta(hours=1)),
            revoked_at=revoked_at,
            revoked_by=lo if revoked_at else None,
            revoked_reason="operator revoked link" if revoked_at else None,
            lo_key_kind="coldkey" if coldkey_proof else "hotkey",
            hi_key_kind="coldkey" if coldkey_proof else "hotkey",
            lo_signer=lo.replace("hotkey-", "owner-") if coldkey_proof else lo,
            hi_signer=hi.replace("hotkey-", "owner-") if coldkey_proof else hi,
            lo_signature="ab" * 64,
            hi_signature="cd" * 64,
        )
    )
    await session.flush()


async def test_paid_owner_survives_hotkey_and_name_changes(session: AsyncSession):
    async with session.begin():
        anchor = await paid_attempt(session)
        for _ in range(2):
            await paid_attempt(session, lineage=anchor)
    guidance = await _compare(session, hotkey="new-hotkey")
    assert guidance.retry_at is not None
    assert guidance.lineage_agent_id == anchor
    assert guidance.completed_low_information_attempts == 3
    other = await _compare(session, coldkey="unrelated-owner", hotkey="hotkey-a")
    assert other.classification == "first_submission"


async def test_only_active_direct_signed_links_expand_history(session: AsyncSession):
    async with session.begin():
        anchor = await paid_attempt(session, hotkey="hotkey-b", coldkey="owner-b")
        await _link(session, "hotkey-a", "hotkey-b")
        await _link(session, "hotkey-b", "hotkey-c")
        await _link(session, "hotkey-a", "hotkey-d", revoked_at=NOW)
        await paid_attempt(session, hotkey="hotkey-c", coldkey="owner-c")
        await paid_attempt(session, hotkey="hotkey-d", coldkey="owner-d")
    guidance = await _compare(session)
    assert guidance.lineage_agent_id == anchor
    assert guidance.completed_low_information_attempts == 1
    assert guidance.retry_at is None


async def test_hotkey_control_does_not_transfer_a_previous_payers_budget(
    session: AsyncSession,
):
    async with session.begin():
        await paid_attempt(session, hotkey="hotkey-b", coldkey="previous-owner")
        await _link(session, "hotkey-a", "hotkey-b", coldkey_proof=False)
    guidance = await _compare(session)
    assert guidance.classification == "first_submission"


async def test_signed_coldkey_scope_survives_hotkey_rotation(session: AsyncSession):
    async with session.begin():
        previous = await paid_attempt(
            session, hotkey="rotated-hotkey", coldkey="owner-b"
        )
        await _link(session, "hotkey-a", "hotkey-b")
    guidance = await _compare(session, hotkey="another-rotated-hotkey")
    assert guidance.reference_agent_id == previous


async def test_replay_does_not_use_future_attestations_or_feedback(
    session: AsyncSession,
):
    async with session.begin():
        await paid_attempt(session, hotkey="hotkey-b", coldkey="owner-b")
        await _link(
            session, "hotkey-a", "hotkey-b", created_at=NOW + timedelta(seconds=1)
        )
        current = await paid_attempt(session, submitted_at=NOW - timedelta(seconds=10))
    guidance = await _compare(session)
    assert guidance.reference_agent_id == current
    assert guidance.completed_low_information_attempts == 0


async def test_old_exact_lineage_cannot_be_hidden_by_history_page(
    session: AsyncSession,
):
    async with session.begin():
        anchor = await paid_attempt(session, submitted_at=NOW - timedelta(hours=2))
        await paid_attempt(
            session, lineage=anchor, submitted_at=NOW - timedelta(minutes=100)
        )
        await paid_attempt(
            session, lineage=anchor, submitted_at=NOW - timedelta(minutes=90)
        )
        for i in range(105):
            source = profile()
            source["runtime_hash"] = f"{i + 1:064x}"
            source["fingerprint"] = None
            await paid_attempt(
                session,
                source=source,
                classification="material_new_work",
                submitted_at=NOW - timedelta(minutes=80, seconds=i),
            )
    guidance = await _compare(session)
    assert guidance.lineage_agent_id == anchor
    # The feedback is older than the cooldown, so the budget remains recorded
    # while the next attempt is permitted.
    assert guidance.completed_low_information_attempts == 3
    assert guidance.retry_at is None


@pytest.mark.parametrize("outcome", ["failed", "expired"])
async def test_screener_fault_releases_capacity(session: AsyncSession, outcome: str):
    async with session.begin():
        anchor = await paid_attempt(session)
        await paid_attempt(session, lineage=anchor)
        await paid_attempt(
            session,
            lineage=anchor,
            outcome=outcome,
            submitted_at=NOW - timedelta(minutes=5),
        )
    guidance = await _compare(session)
    assert guidance.classification == "infrastructure_retry"
    assert guidance.completed_low_information_attempts == 2
    assert guidance.reserved_low_information_attempts == 0
    assert guidance.retry_at is None


async def test_signed_owner_reservations_serialize_at_budget_boundary(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
):
    async with session.begin():
        anchor = await paid_attempt(session)
        await paid_attempt(session, lineage=anchor)
        await _link(session, "hotkey-a", "hotkey-b")
    ready = asyncio.Event()

    async def reserve(hotkey, coldkey, first):
        if not first:
            await ready.wait()
        async with session_maker() as connection, connection.begin():
            await lock_attempt_owner(
                connection, hotkey=hotkey, coldkey=coldkey, netuid=118, now=NOW
            )
            guidance = await _compare(connection, hotkey=hotkey, coldkey=coldkey)
            if first:
                assert guidance.retry_at is None
                await reserve_upload_admission(
                    connection,
                    miner_hotkey=hotkey,
                    miner_coldkey=coldkey,
                    sha256="d" * 64,
                    settings=EffectiveSubmissionSettings(
                        revision=1, cooldown_seconds=3600, payment_address="destination"
                    ),
                    now=NOW - timedelta(seconds=1),
                    skip_owner_cooldown=True,
                    attempt_context={
                        "profile": profile(),
                        "guidance": guidance.model_dump(mode="json"),
                    },
                )
                ready.set()
                await asyncio.sleep(0.02)
            return guidance

    first, second = await asyncio.gather(
        reserve("hotkey-a", "owner-a", True),
        reserve("hotkey-b", "owner-b", False),
    )
    assert first.retry_at is None
    assert second.reserved_low_information_attempts == 1
    assert second.retry_at is not None


async def test_replay_refuses_history_collected_under_different_settings(
    session: AsyncSession,
):
    async with session.begin():
        await paid_attempt(session)
    guidance = await compare_attempt(
        session,
        profile=profile(),
        hotkey="hotkey-a",
        coldkey="owner-a",
        netuid=118,
        settings=AttemptControlSettings(low_information_limit=5),
        revision=1,
        now=NOW,
        require_compatible_history=True,
        include_reservations=False,
    )
    assert guidance.classification == "inconclusive"


@pytest.mark.parametrize("reason", ["infrastructure", "scoring_error"])
async def test_validator_fault_releases_pending_usage(session: AsyncSession, reason):
    async with session.begin():
        anchor = await paid_attempt(session)
        await paid_attempt(session, lineage=anchor)
        candidate = await paid_attempt(
            session,
            lineage=anchor,
            outcome="pending",
            submitted_at=NOW - timedelta(minutes=5),
        )
        session.add(
            ValidatorTicket(
                agent_id=candidate,
                validator_hotkey="validator",
                bench_version=7,
                purpose="canonical_quorum",
                purpose_revision=1,
                issued_at=NOW - timedelta(minutes=4),
                deadline=NOW + timedelta(hours=1),
                failure_reason=reason,
                failed_at=NOW - timedelta(minutes=3),
            )
        )
    guidance = await _compare(session)
    assert guidance.classification == "infrastructure_retry"
    assert guidance.completed_low_information_attempts == 2
    assert guidance.reserved_low_information_attempts == 0
    assert guidance.retry_at is None


async def test_incompatible_build_falls_back_to_observable_shadow(
    session: AsyncSession,
):
    async with session.begin():
        session.add(
            SubmissionAttemptSettingsRevision(
                parent_revision=0,
                settings=AttemptControlSettings(mode="enforce").model_dump(),
                calibration_id=None,
                actor="operator",
                reason="stale release calibration",
            )
        )
    effective, revision = await attempt_settings(session)
    assert effective.mode == "shadow"
    assert revision > 0


async def test_equal_timestamps_are_inconclusive_in_replay(session: AsyncSession):
    async with session.begin():
        await paid_attempt(session, submitted_at=NOW)
        candidate = await paid_attempt(session, submitted_at=NOW)
    guidance = await _compare(
        session, replay_agent_id=candidate, include_reservations=False
    )
    assert guidance.classification == "inconclusive"


async def test_mixed_benchmark_scores_do_not_form_completed_feedback(
    session: AsyncSession,
):
    async with session.begin():
        agent = await paid_attempt(session, outcome="pending")
        scores = [
            Score(
                agent_id=agent,
                validator_hotkey=f"validator-{version}",
                bench_version=version,
                run_id="run",
                seed=42,
                composite=0.7,
                tool_mean=0.7,
                memory_mean=0.7,
                median_ms=1,
                n=30,
                signature="ab" * 64,
                generated_at=NOW - timedelta(minutes=9),
                created_at=NOW - timedelta(minutes=9),
            )
            for version in (7, 8, 9)
        ]
        session.add_all(scores)
    mixed = await _compare(session)
    assert mixed.completed_low_information_attempts == 0
    assert mixed.reserved_low_information_attempts == 1
    await session.rollback()
    async with session.begin():
        # Rollback expires ORM attributes; update by the stable identity.
        from sqlalchemy import update

        await session.execute(
            update(Score).where(Score.agent_id == agent).values(bench_version=7)
        )
    completed = await _compare(session)
    assert completed.completed_low_information_attempts == 1
    assert completed.reserved_low_information_attempts == 0
