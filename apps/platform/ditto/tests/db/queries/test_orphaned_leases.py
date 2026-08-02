"""What the platform may conclude about a run it evicted but did not stop.

Every case here is a claim about one asymmetry: an operator eviction ends the
platform's lease instantly, and ends the validator's container not at all. The
rule under test is that the fleet view may say "still running" only on the
validator's own positive claim, may say "free" only when the reporter is new
enough for absence to mean something, and must otherwise say it does not know.

The protocol-15 class is the point of the whole module. A pre-16 reporter omits
a claimed-but-quiet slot entirely, so silence there is not evidence of an idle
slot -- and rendering it as idle is the bug this exists to fix. It must not be
rendered as *running* either.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import Agent, ValidatorHeartbeat, ValidatorLeaseAudit
from ditto.db.queries.lease_liveness import (
    ACTION_FORCE_EXPIRED,
    ACTION_OPERATOR_EVICTED,
    IDLE_EVIDENCE_MAX_AGE,
)
from ditto.db.queries.orphaned_leases import (
    FALLBACK_RUN_HORIZON,
    INDETERMINATE_HORIZON_GRACE,
    ORPHAN_REASON_EVIDENCE_PREDATES_EVICTION,
    ORPHAN_REASON_HEARTBEAT_MISSING,
    ORPHAN_REASON_HEARTBEAT_STALE,
    ORPHAN_REASON_NO_OCCUPANCY_REPORT,
    ORPHAN_REASON_PRE_V16_OMITS_QUIET_SLOT,
    ORPHAN_REASON_SLOT_CLAIMED_BY_ANOTHER_AGENT,
    ORPHAN_REASON_SLOT_NO_LONGER_CLAIMED,
    ORPHAN_REASON_VALIDATOR_CLAIMS_SLOT,
    list_orphaned_leases,
)

# Anchored on the real incident: nine bench-7 submissions evicted at 20:56Z on
# 2026-07-27, revoking five live leases across three validators, each of whose
# containers kept running toward a deadline ~45-65 minutes out.
_NOW = datetime(2026, 7, 27, 21, 19, 6, tzinfo=UTC)
_EVICTED_AT = datetime(2026, 7, 27, 20, 56, 1, tzinfo=UTC)
_DEADLINE = datetime(2026, 7, 27, 22, 2, 59, tzinfo=UTC)
_HOTKEY = "5HmP9732JFjnut2RY9yg4Gz2qJ38vF8xFwZb5dQVPF7FsmZz"
_SLOT = "slot-0"
_AGENT = UUID("67dbaab7-29ae-4f02-bb2e-ba01dc18c241")
_OTHER_AGENT = UUID("45d515f7-7d9f-495c-bc17-1b818adbf2c6")


async def _seed_agent(
    session: AsyncSession, *, agent_id: UUID = _AGENT, name: str = "mnemox-v55"
) -> None:
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5Miner",
                name=name,
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW - timedelta(days=1),
            )
        )


async def _seed_eviction(
    session: AsyncSession,
    *,
    hotkey: str = _HOTKEY,
    slot_id: str = _SLOT,
    agent_id: UUID = _AGENT,
    recorded_at: datetime = _EVICTED_AT,
    original_deadline: datetime | None = _DEADLINE,
    action: str = ACTION_OPERATOR_EVICTED,
) -> UUID:
    """One eviction ledger row, shaped exactly like the five production rows."""
    audit_id = uuid4()
    evidence: dict = {
        "idle": True,
        "reason": "operator_evicted_occupied_progressing",
        "context": "admin_queue_eviction",
        "slot_id": slot_id,
        "action": action,
        "attempt_count": 1,
    }
    if original_deadline is not None:
        evidence["original_deadline"] = original_deadline.isoformat()
    async with session.begin():
        session.add(
            ValidatorLeaseAudit(
                audit_id=audit_id,
                agent_id=agent_id,
                validator_hotkey=hotkey,
                slot_id=slot_id,
                bench_version=7,
                action=action,
                reason="operator_evicted_occupied_progressing",
                context="admin_queue_eviction",
                evidence=evidence,
                recorded_at=recorded_at,
            )
        )
    return audit_id


async def _seed_heartbeat(
    session: AsyncSession,
    *,
    hotkey: str = _HOTKEY,
    protocol_version: int = 16,
    seen_at: datetime | None = None,
    claimed_slots: list[dict] | None = None,
) -> None:
    """The latest signed report.

    ``benchmark_capacity`` is deliberately left empty even when the validator is
    claiming a slot: production proves that is the real shape. The ingest filter
    drops any slot it cannot match to a *live* ticket, and an evicted lease has
    none by definition -- so 23 minutes after the incident the two orphaned
    hosts had ``benchmark_capacity.active = []`` and their orphaned run visible
    only in ``claimed_slots``. A test that seeded the orphan into the capacity
    blob would be testing a state that cannot occur.
    """
    moment = seen_at if seen_at is not None else _NOW - timedelta(seconds=5)
    async with session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=hotkey,
                software_version="0.35.2",
                protocol_version=protocol_version,
                code_digest="ab" * 32,
                state="running_benchmark",
                first_seen_at=moment - timedelta(days=1),
                reported_at=moment,
                seen_at=moment,
                signature="cd" * 64,
                benchmark_capacity={
                    "configured_slots": 4,
                    "healthy_slots": ["slot-0", "slot-1", "slot-2", "slot-3"],
                    "admission": "accepting",
                    "active": [],
                },
                claimed_slots=claimed_slots,
            )
        )


def _claim(*, slot_id: str = _SLOT, agent_id: UUID = _AGENT) -> dict:
    return {"slot_id": slot_id, "agent_id": str(agent_id)}


class TestStillRunning:
    async def test_validator_claim_survives_the_capacity_filter(
        self, session: AsyncSession
    ) -> None:
        """The production case: the claim is the only place the orphan is left."""
        await _seed_agent(session)
        audit_id = await _seed_eviction(session)
        await _seed_heartbeat(session, claimed_slots=[_claim()])

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert len(orphans) == 1
        orphan = orphans[0]
        assert orphan.audit_id == audit_id
        assert orphan.state == "still_running"
        assert orphan.reason == ORPHAN_REASON_VALIDATOR_CLAIMS_SLOT
        assert orphan.validator_hotkey == _HOTKEY
        assert orphan.slot_id == _SLOT
        assert orphan.agent_id == _AGENT
        assert orphan.agent_name == "mnemox-v55"
        assert orphan.bench_version == 7
        assert orphan.evicted_at == _EVICTED_AT
        assert orphan.original_deadline == _DEADLINE
        assert orphan.protocol_version == 16
        # ~23 minutes of a benchmark's worth of CPU that cannot produce a score.
        assert orphan.orphaned_for_seconds == round(
            (_NOW - _EVICTED_AT).total_seconds(), 3
        )

    async def test_presence_is_admissible_on_protocol_15_too(
        self, session: AsyncSession
    ) -> None:
        """Only *absence* is protocol-dependent. A claim is a claim."""
        await _seed_agent(session)
        await _seed_eviction(session)
        await _seed_heartbeat(session, protocol_version=15, claimed_slots=[_claim()])

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.state, o.reason) for o in orphans] == [
            ("still_running", ORPHAN_REASON_VALIDATOR_CLAIMS_SLOT)
        ]

    async def test_positive_evidence_outlives_the_original_deadline(
        self, session: AsyncSession
    ) -> None:
        """A container still held past its own deadline is the worst case, not a
        reason to stop reporting it. Only unproven orphans expire from the view.
        """
        await _seed_agent(session)
        await _seed_eviction(session)
        late = _DEADLINE + timedelta(hours=1)
        await _seed_heartbeat(
            session, seen_at=late - timedelta(seconds=5), claimed_slots=[_claim()]
        )

        orphans = await list_orphaned_leases(session, now=late, live_slots=set())

        assert [o.state for o in orphans] == ["still_running"]


class TestReleased:
    async def test_protocol_16_absence_proves_the_container_exited(
        self, session: AsyncSession
    ) -> None:
        """A v16 reporter announces a slot from claim to release, so silence is
        real evidence. This is the slot that should keep rendering as idle."""
        await _seed_agent(session)
        await _seed_eviction(session)
        await _seed_heartbeat(session, protocol_version=16, claimed_slots=[])

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.state, o.reason) for o in orphans] == [
            ("released", ORPHAN_REASON_SLOT_NO_LONGER_CLAIMED)
        ]

    async def test_another_agent_on_the_slot_ends_the_orphan_on_any_protocol(
        self, session: AsyncSession
    ) -> None:
        """A slot runs one benchmark at a time, so this is presence, not absence."""
        await _seed_agent(session)
        await _seed_eviction(session)
        await _seed_heartbeat(
            session,
            protocol_version=15,
            claimed_slots=[_claim(agent_id=_OTHER_AGENT)],
        )

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.state, o.reason) for o in orphans] == [
            ("released", ORPHAN_REASON_SLOT_CLAIMED_BY_ANOTHER_AGENT)
        ]


class TestIndeterminate:
    async def test_protocol_15_silence_is_never_read_as_idle_or_as_running(
        self, session: AsyncSession
    ) -> None:
        """The constraint this module exists for. A pre-16 reporter omits a
        claimed-but-quiet slot, so its silence cannot be turned into either
        confident answer -- not the false 'Idle' that started this, and not a
        false 'still running' in the other direction."""
        await _seed_agent(session)
        await _seed_eviction(session)
        await _seed_heartbeat(session, protocol_version=15, claimed_slots=[])

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert len(orphans) == 1
        assert orphans[0].state == "indeterminate"
        assert orphans[0].reason == ORPHAN_REASON_PRE_V16_OMITS_QUIET_SLOT
        # Carried through so the surface can say *why* it cannot tell.
        assert orphans[0].protocol_version == 15

    async def test_no_heartbeat_row_is_unknown(self, session: AsyncSession) -> None:
        await _seed_agent(session)
        await _seed_eviction(session)

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.state, o.reason, o.protocol_version) for o in orphans] == [
            ("indeterminate", ORPHAN_REASON_HEARTBEAT_MISSING, None)
        ]

    async def test_stale_heartbeat_is_unknown_even_when_it_claims_the_slot(
        self, session: AsyncSession
    ) -> None:
        """Same freshness bar as the revocation gate: a sample minutes old
        describes a world that may already be gone, in either direction."""
        await _seed_agent(session)
        await _seed_eviction(session)
        await _seed_heartbeat(
            session,
            seen_at=_NOW - IDLE_EVIDENCE_MAX_AGE - timedelta(seconds=1),
            claimed_slots=[_claim()],
        )

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.state, o.reason) for o in orphans] == [
            ("indeterminate", ORPHAN_REASON_HEARTBEAT_STALE)
        ]

    async def test_a_sample_older_than_the_eviction_cannot_testify_about_it(
        self, session: AsyncSession
    ) -> None:
        await _seed_agent(session)
        await _seed_eviction(session, recorded_at=_NOW - timedelta(seconds=30))
        await _seed_heartbeat(
            session, seen_at=_NOW - timedelta(seconds=60), claimed_slots=[]
        )

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.state, o.reason) for o in orphans] == [
            ("indeterminate", ORPHAN_REASON_EVIDENCE_PREDATES_EVICTION)
        ]

    async def test_a_validator_advertising_no_occupancy_is_unknown(
        self, session: AsyncSession
    ) -> None:
        """Pre-v10 reporters (and any heartbeat whose work payload failed to
        validate) store no claim at all."""
        await _seed_agent(session)
        await _seed_eviction(session)
        await _seed_heartbeat(session, protocol_version=6, claimed_slots=None)

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.state, o.reason) for o in orphans] == [
            ("indeterminate", ORPHAN_REASON_NO_OCCUPANCY_REPORT)
        ]

    async def test_unproven_orphans_stop_being_reported_past_the_deadline(
        self, session: AsyncSession
    ) -> None:
        """Once the deadline the validator itself cached has passed, its own run
        timeout has fired, so 'this might still be running' contradicts the
        clock."""
        await _seed_agent(session)
        await _seed_eviction(session)
        late = _DEADLINE + INDETERMINATE_HORIZON_GRACE + timedelta(minutes=1)
        await _seed_heartbeat(
            session,
            protocol_version=15,
            seen_at=late - timedelta(seconds=5),
            claimed_slots=[],
        )

        assert await list_orphaned_leases(session, now=late, live_slots=set()) == []

    async def test_a_deadlineless_audit_row_falls_back_to_a_bounded_horizon(
        self, session: AsyncSession
    ) -> None:
        await _seed_agent(session)
        await _seed_eviction(session, original_deadline=None)
        inside = _EVICTED_AT + FALLBACK_RUN_HORIZON - timedelta(minutes=1)
        await _seed_heartbeat(
            session,
            protocol_version=15,
            seen_at=inside - timedelta(seconds=5),
            claimed_slots=[],
        )

        orphans = await list_orphaned_leases(session, now=inside, live_slots=set())

        assert [(o.state, o.original_deadline) for o in orphans] == [
            ("indeterminate", None)
        ]


class TestLedgerReading:
    async def test_no_evictions_reads_nothing(self, session: AsyncSession) -> None:
        await _seed_heartbeat(session, claimed_slots=[_claim()])

        assert await list_orphaned_leases(session, now=_NOW, live_slots=set()) == []

    async def test_automatic_revocations_are_not_evictions(
        self, session: AsyncSession
    ) -> None:
        """``force_expired`` is the inferred-idle path. It reissues the lease, so
        it is not the operator decision this view describes."""
        await _seed_agent(session)
        await _seed_eviction(session, action=ACTION_FORCE_EXPIRED)
        await _seed_heartbeat(session, claimed_slots=[_claim()])

        assert await list_orphaned_leases(session, now=_NOW, live_slots=set()) == []

    async def test_a_relaeased_slot_the_platform_reissued_is_dropped(
        self, session: AsyncSession
    ) -> None:
        """The slot now renders real work, so it is no longer a false idle."""
        await _seed_agent(session)
        await _seed_eviction(session)
        await _seed_heartbeat(session, claimed_slots=[_claim()])

        assert (
            await list_orphaned_leases(session, now=_NOW, live_slots={(_HOTKEY, _SLOT)})
            == []
        )

    async def test_the_newest_eviction_wins_per_slot(
        self, session: AsyncSession
    ) -> None:
        """Two evictions on one slot describe one host, not two."""
        await _seed_agent(session)
        await _seed_agent(session, agent_id=_OTHER_AGENT, name="mnemo7-v3")
        await _seed_eviction(
            session, agent_id=_OTHER_AGENT, recorded_at=_EVICTED_AT - timedelta(hours=1)
        )
        newest = await _seed_eviction(session, agent_id=_AGENT)
        await _seed_heartbeat(session, claimed_slots=[_claim()])

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.audit_id, o.agent_id) for o in orphans] == [(newest, _AGENT)]

    async def test_evictions_outside_the_lookback_are_not_read(
        self, session: AsyncSession
    ) -> None:
        await _seed_agent(session)
        await _seed_eviction(session, recorded_at=_NOW - timedelta(days=2))
        await _seed_heartbeat(session, claimed_slots=[_claim()])

        assert await list_orphaned_leases(session, now=_NOW, live_slots=set()) == []

    async def test_one_agent_evicted_from_several_validators_reports_each_host(
        self, session: AsyncSession
    ) -> None:
        """Quorum means one submission holds a slot on several validators, and
        each of those hosts is separately still burning CPU. The production
        incident evicted agent 45d515f7 from two validators at the same instant.
        """
        second_hotkey = "5Cg3DiRfrgzB1XzN7VuqQNchTgZ8PzPbphMKmVvHobWSL118"
        await _seed_agent(session, agent_id=_OTHER_AGENT, name="mnemox-v55")
        await _seed_eviction(session, slot_id="slot-2", agent_id=_OTHER_AGENT)
        await _seed_eviction(
            session, hotkey=second_hotkey, slot_id="slot-2", agent_id=_OTHER_AGENT
        )
        await _seed_heartbeat(
            session, claimed_slots=[_claim(slot_id="slot-2", agent_id=_OTHER_AGENT)]
        )
        await _seed_heartbeat(
            session,
            hotkey=second_hotkey,
            protocol_version=15,
            claimed_slots=[],
        )

        orphans = await list_orphaned_leases(session, now=_NOW, live_slots=set())

        assert [(o.validator_hotkey, o.state) for o in orphans] == [
            (second_hotkey, "indeterminate"),
            (_HOTKEY, "still_running"),
        ]
