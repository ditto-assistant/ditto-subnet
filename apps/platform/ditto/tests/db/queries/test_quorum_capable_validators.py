"""The quorum-capable fleet is the fleet that can actually be issued the work.

:func:`~ditto.db.queries.queue_order.quorum_capable_validator_hotkeys` is the
denominator of the owner pin's reachability ceiling. In production on
2026-10-13 it counted every fresh heartbeat advertising v13 -- including a
validator outside the pinned scorer cohort (never issued v13 work) and one the
operator had paused. Five "capable" validators kept a 2/3 generation looking
reachable after its only remaining cohort member had spent its retry budget, so
the dead generation stayed pinned and held its owner's newer upload at
``owner_serialized``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    EvaluationPayment,
    Score,
    V13ScorerCohortPin,
    ValidatorHeartbeat,
    ValidatorSlotSettingsRevision,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_admission import activated_rollout_for_version
from ditto.db.queries.queue_order import (
    preview_queue_order,
    quorum_capable_validator_hotkeys,
)
from ditto.db.queries.tickets import (
    MAX_ATTEMPTS_PER_VERSION,
    get_score_priority_floors,
    issue_ticket,
)
from ditto.tests.db.queries.test_benchmark_rollout import _heartbeat

pytestmark = pytest.mark.asyncio

_V13 = 13
_NOW = datetime.now(UTC).replace(microsecond=0)
_TTL = timedelta(minutes=90)
_SOURCE = "a" * 40
_KEYS = ["DITTOBENCH_DB"]
_PACKET = {
    "source_revision": _SOURCE,
    "release_descriptor_digest": "sha256:" + "b" * 64,
    "scorer_image_digest": "sha256:" + "c" * 64,
    "scorer_env_sha256": hashlib.sha256(
        ("scored-runtime-env-v1\n13\n" + _SOURCE + "\n" + "\n".join(_KEYS)).encode()
    ).hexdigest(),
    "injected_keys": _KEYS,
}
# SS58-shaped, because the slot policy validates paused hotkeys.
_COHORT = (
    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
)
# Both advertise v13 on a fresh heartbeat. Neither can ever be issued it.
_OUTSIDER = "5Cg3DiRfvXyfz4ZrNvkMJE2KfaZQMNcfQbSdtiDmLD1BNqKd"
_PAUSED = "5CqJAjSjv8fjF9uAQpDLyfN1hZEvBjwpFgcGeLbYpcbSaD1C"
_OWNER_COLDKEY = "5HarryOwnerColdkey"


def _managed(hotkey: str) -> ValidatorHeartbeat:
    """A v13 heartbeat signing the cohort's shared packet.

    The issuing member must pass ``pinned_validator_allowed``, which demands
    that all three members sign the same managed release.
    """
    row = _heartbeat(hotkey, _NOW, versions=[7, _V13], protocol_version=18)
    row.benchmark_capacity = {
        "configured_slots": 1,
        "healthy_slots": ["slot-0"],
        "admission": "accepting",
        "active": [],
    }
    assert row.stack is not None and row.capabilities is not None
    row.stack["mode"] = "managed"
    row.stack["release_descriptor_digest"] = _PACKET["release_descriptor_digest"]
    for component in row.stack["components"].values():
        component["provenance"] = "signed_descriptor"
        component["image_digest"] = _PACKET["scorer_image_digest"]
        component["source_revision"] = _SOURCE
    row.capabilities["scorer_benchmarks"]["source_revision"] = _SOURCE
    row.capabilities["scorer_benchmarks"]["scored_runtime_env"] = {
        "bench_version": _V13,
        "scope": "scorer-injected-env-only",
        "source_revision": _SOURCE,
        "injected_keys": _KEYS,
        "sha256": _PACKET["scorer_env_sha256"],
    }
    return row


async def _seed_fleet(session: AsyncSession) -> None:
    """The live shape: three cohort members plus two v13-capable extras."""
    async with session.begin():
        session.add_all([_managed(hotkey) for hotkey in _COHORT])
        session.add_all(
            [
                _heartbeat(hotkey, _NOW, versions=[7, _V13], protocol_version=18)
                for hotkey in (_OUTSIDER, _PAUSED)
            ]
        )


async def _pause(session: AsyncSession, *hotkeys: str) -> None:
    settings = ValidatorSlotSettings(paused_validator_hotkeys=list(hotkeys))
    async with session.begin():
        session.add(
            ValidatorSlotSettingsRevision(
                parent_revision=0,
                scope="*",
                settings=settings.model_dump(mode="json"),
                checksum="f" * 64,
                reason="test issuance pause",
                actor="test",
            )
        )


async def _pin_cohort(session: AsyncSession) -> None:
    async with session.begin():
        session.add(
            V13ScorerCohortPin(
                bench_version=_V13,
                hotkeys=list(_COHORT),
                packet=_PACKET,
                slot_settings_revision=1,
                slot_settings_checksum="f" * 64,
                reason="test exact signed scorer cohort",
                actor="test",
            )
        )


async def _capable(session: AsyncSession, *, bench_version: int = _V13) -> set[str]:
    async with session.begin():
        return await quorum_capable_validator_hotkeys(
            session, bench_version=bench_version, now=_NOW
        )


class TestQuorumCapableValidatorHotkeys:
    async def test_before_activation_every_capable_heartbeat_counts(
        self, session: AsyncSession
    ) -> None:
        """No pin, no pause: the version check alone decides, as it always did."""
        await _seed_fleet(session)

        assert await _capable(session) == {*_COHORT, _OUTSIDER, _PAUSED}

    async def test_a_pinned_cohort_excludes_nonmembers(
        self, session: AsyncSession
    ) -> None:
        await _seed_fleet(session)
        await _pin_cohort(session)

        assert await _capable(session) == set(_COHORT)

    async def test_a_paused_validator_is_not_capable(
        self, session: AsyncSession
    ) -> None:
        await _seed_fleet(session)
        await _pause(session, _PAUSED)

        assert await _capable(session) == {*_COHORT, _OUTSIDER}

    async def test_a_paused_cohort_member_is_not_capable(
        self, session: AsyncSession
    ) -> None:
        await _seed_fleet(session)
        await _pin_cohort(session)
        await _pause(session, _COHORT[2])

        assert await _capable(session) == set(_COHORT[:2])

    async def test_the_v13_pin_does_not_narrow_another_version(
        self, session: AsyncSession
    ) -> None:
        await _seed_fleet(session)
        await _pin_cohort(session)

        assert await _capable(session, bench_version=7) == {
            *_COHORT,
            _OUTSIDER,
            _PAUSED,
        }


async def _seed_generation(
    session: AsyncSession, *, name: str, created_at: datetime
) -> UUID:
    """One admitted v13 submission paid for by the shared owner coldkey."""
    agent_id = uuid4()
    hotkey = f"5Harry-{name}"
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=created_at,
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=created_at,
            )
        )
        await session.flush()
        session.add_all(
            [
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_V13,
                    seed=1,
                    sha256="cd" * 32,
                    run_size="full",
                ),
                EvaluationPayment(
                    block_hash=f"0x{uuid4().hex}",
                    extrinsic_index=0,
                    agent_id=agent_id,
                    miner_hotkey=hotkey,
                    miner_coldkey=_OWNER_COLDKEY,
                    amount_rao=1,
                    tao_usd_rate=Decimal("1"),
                    dest_address="5Destination",
                    timestamp=created_at,
                ),
            ]
        )
    return agent_id


async def _seed_two_of_three(
    session: AsyncSession, agent_id: UUID, *, third_exhausted: bool = True
) -> None:
    """Scored by two cohort members; the third has burned its budget, or not.

    ``MAX_ATTEMPTS_PER_VERSION`` is one, so a single expired lease with no
    operator grant is a spent budget.
    """
    started = _NOW - timedelta(days=2)
    async with session.begin():
        for index, scorer in enumerate(_COHORT[:2]):
            issued_at = started + timedelta(hours=index)
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_V13,
                    validator_hotkey=scorer,
                    slot_id="slot-0",
                    status=TicketStatus.SCORED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=issued_at,
                    deadline=issued_at + _TTL,
                    attempt_count=1,
                )
            )
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=_V13,
                    validator_hotkey=scorer,
                    run_id=f"run-{index}",
                    signature=None,
                    seed=index,
                    composite=0.5,
                    tool_mean=0.5,
                    memory_mean=0.5,
                    median_ms=100,
                    n=114,
                    details={"bench_version": _V13},
                    generated_at=issued_at + timedelta(minutes=30),
                )
            )
        if not third_exhausted:
            return
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                bench_version=_V13,
                validator_hotkey=_COHORT[2],
                slot_id="slot-0",
                status=TicketStatus.EXPIRED,
                purpose=TicketPurpose.CANONICAL_QUORUM,
                purpose_revision=1,
                issued_at=started + timedelta(hours=3),
                deadline=started + timedelta(hours=3) + _TTL,
                attempt_count=MAX_ATTEMPTS_PER_VERSION,
            )
        )


class TestDeadCohortGenerationReleasesItsOwner:
    """The live incident: Harry's v13 generation held Harry's newer upload."""

    async def _seed(self, session: AsyncSession) -> tuple[UUID, UUID]:
        await _seed_fleet(session)
        dead = await _seed_generation(
            session, name="dead", created_at=_NOW - timedelta(days=3)
        )
        newer = await _seed_generation(
            session, name="newer", created_at=_NOW - timedelta(hours=1)
        )
        await _seed_two_of_three(session, dead)
        await _pin_cohort(session)
        await _pause(session, _PAUSED)
        return dead, newer

    async def test_preview_shows_the_newer_upload_leasable(
        self, session: AsyncSession
    ) -> None:
        dead, newer = await self._seed(session)
        async with session.begin():
            continuation, provisional = await get_score_priority_floors(
                session, bench_version=_V13
            )
            entries = await preview_queue_order(
                session,
                bench_version=_V13,
                now=_NOW,
                agent_ids=[dead, newer],
                score_continuation_floor=continuation,
                provisional_contender_floor=provisional,
                rollout=await activated_rollout_for_version(
                    session, bench_version=_V13
                ),
            )

        assert entries[newer].gate is None
        assert entries[newer].leasable

    async def test_a_cohort_member_leases_the_newer_upload(
        self, session: AsyncSession
    ) -> None:
        """Strict owner rail, so only the pin decides who holds the owner's slot.

        At a ceiling of one the last-resort pass cannot admit a second
        generation, so the sibling is leased only if the dead one is unpinned.
        """
        _, newer = await self._seed(session)
        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey=_COHORT[0],
                now=_NOW,
                ttl=_TTL,
                bench_version=_V13,
                owner_concurrent_submission_limit=1,
            )

        assert ticket is not None
        assert ticket.agent_id == newer

    async def test_a_reachable_generation_stays_pinned(
        self, session: AsyncSession
    ) -> None:
        """No regression: while the third member can still score, the pin holds.

        Same fleet and same 2/3 progress, but the remaining cohort member has
        never been leased the generation, so it can still finish.
        """
        await _seed_fleet(session)
        pinned = await _seed_generation(
            session, name="pinned", created_at=_NOW - timedelta(days=3)
        )
        newer = await _seed_generation(
            session, name="newer", created_at=_NOW - timedelta(hours=1)
        )
        await _seed_two_of_three(session, pinned, third_exhausted=False)
        await _pin_cohort(session)
        await _pause(session, _PAUSED)
        async with session.begin():
            continuation, provisional = await get_score_priority_floors(
                session, bench_version=_V13
            )
            entries = await preview_queue_order(
                session,
                bench_version=_V13,
                now=_NOW,
                agent_ids=[pinned, newer],
                score_continuation_floor=continuation,
                provisional_contender_floor=provisional,
                rollout=None,
            )

        assert entries[pinned].gate is None
        assert entries[newer].gate == "owner_serialized"
