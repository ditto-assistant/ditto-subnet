"""Deterministic trigger tests for deferred source review."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.queue_policy_settings import DeferredSourceReviewSettings
from ditto.api_server.deferred_source_review import (
    DeferredReviewDecision,
    evaluate_deferred_review,
    evaluate_integrity_double_check,
    is_no_finding_reason_code,
    public_deferred_review_triggers,
    public_review_conclusion,
)
from ditto.api_server.endpoints.validator import (
    _deferred_screening_attempt,
    _evaluate_and_record_deferred_review,
    _evaluate_and_record_integrity_double_check,
    _held_post_score_review_composites,
    _record_deferred_review_decision,
)
from ditto.db.models import (
    Agent,
    AthReview,
    AthReviewAction,
    ScoreAuditEntry,
    ScreeningAttempt,
    ScreeningQuarantine,
)
from ditto.db.queries.scores import LedgerRow
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION
from ditto_screening_protocol.models import ScreenReviewAudit

# Seeded versions mean "the version the platform REQUIRES" — the floor in the
# default no-scheduled-activation state.
SCREENING_POLICY_VERSION = SCREENING_FLOOR_POLICY_VERSION


def _row(
    index: int,
    composite: float,
    *,
    tool: float | None = None,
    memory: float | None = None,
    eligible: bool = True,
) -> LedgerRow:
    return LedgerRow(
        miner_hotkey=f"miner-{index}",
        agent_id=UUID(int=index + 1),
        composite=composite,
        tool_mean=tool if tool is not None else composite,
        memory_mean=memory if memory is not None else composite,
        first_seen=datetime(2026, 8, 1, tzinfo=UTC) + timedelta(seconds=index),
        sha256=f"{index:064x}",
        size_bytes=100,
        run_id=f"run-{index}",
        seed=index,
        validator_hotkey="validator",
        signature=None,
        status=AgentStatus.SCORED,
        bench_version=8,
        n=351,
        eligible=eligible,
    )


def test_top_five_is_fixed_even_when_anomaly_cohort_is_too_small() -> None:
    ledger = [_row(index, 0.9 - index / 100) for index in range(5)]
    decision = evaluate_deferred_review(
        agent_id=ledger[-1].agent_id,
        ledger=ledger,
        settings=DeferredSourceReviewSettings(
            mode="enforce",
            min_cohort_size=8,
        ),
    )

    assert decision.triggered is True
    assert decision.triggers == ("top_five",)
    assert decision.rank == 5
    assert decision.evidence["anomaly_unavailable"] == "cohort_too_small"


def test_robust_axis_anomaly_triggers_outside_top_five() -> None:
    peers = [_row(index, 0.50, tool=0.50, memory=0.50) for index in range(8)]
    candidate = _row(20, 0.49, tool=0.90, memory=0.49)
    ledger = [*peers, candidate]
    decision = evaluate_deferred_review(
        agent_id=candidate.agent_id,
        ledger=ledger,
        settings=DeferredSourceReviewSettings(
            mode="enforce",
            min_cohort_size=8,
            min_axis_delta=0.15,
        ),
    )

    assert decision.rank == 9
    assert decision.triggers == ("tool_anomaly",)
    assert decision.evidence["thresholds"] == {
        "composite": {"median": 0.5, "mad": 0.0, "threshold": 0.6},
        "tool_mean": {"median": 0.5, "mad": 0.0, "threshold": 0.65},
        "memory_mean": {"median": 0.5, "mad": 0.0, "threshold": 0.65},
    }


def test_later_score_transition_can_move_candidate_into_top_five() -> None:
    candidate = _row(20, 0.44)
    initial = [_row(index, 0.90 - index / 100) for index in range(6)] + [candidate]
    first = evaluate_deferred_review(
        agent_id=candidate.agent_id,
        ledger=initial,
        settings=DeferredSourceReviewSettings(mode="enforce"),
    )
    promoted = _row(20, 0.865)
    later = [*initial[:4], promoted, initial[4], initial[5]]
    second = evaluate_deferred_review(
        agent_id=promoted.agent_id,
        ledger=later,
        settings=DeferredSourceReviewSettings(mode="enforce"),
    )

    assert first.triggered is False
    assert first.rank == 7
    assert second.triggered is True
    assert second.triggers == ("top_five",)
    assert second.rank == 5


@pytest.mark.asyncio
async def test_other_agent_replacement_rechecks_promoted_deferred_peer(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    ledger = [
        _row(0, 0.90),
        _row(1, 0.89),
        _row(2, 0.88),
        _row(3, 0.87),
        _row(4, 0.86),  # promoted into rank five by another row's replacement
        _row(5, 0.70),  # the row whose accepted replacement triggered recheck
    ]
    promoted = Agent(
        agent_id=ledger[4].agent_id,
        miner_hotkey=ledger[4].miner_hotkey,
        name="promoted-peer",
        sha256=ledger[4].sha256,
        status=AgentStatus.SCORED,
        screening_policy_version=SCREENING_POLICY_VERSION,
    )
    updated = Agent(
        agent_id=ledger[5].agent_id,
        miner_hotkey=ledger[5].miner_hotkey,
        name="updated-row",
        sha256=ledger[5].sha256,
        status=AgentStatus.SCORED,
        screening_policy_version=SCREENING_POLICY_VERSION,
    )
    admission = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=promoted.agent_id,
        screener_hotkey="screener",
        policy_version=SCREENING_POLICY_VERSION,
        status="passed",
        started_at=now - timedelta(hours=1),
        deadline=now - timedelta(minutes=30),
        finished_at=now - timedelta(minutes=45),
        reason_code="deferred-mechanical-admission",
        build_only=True,
    )
    async with session.begin():
        session.add_all([promoted, updated, admission])

    async def _ledger(*_args: object, **_kwargs: object) -> list[LedgerRow]:
        return ledger

    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _ledger
    )
    async with session.begin():
        await _evaluate_and_record_deferred_review(
            session,
            agent=updated,
            bench_version=8,
            score_count=3,
            settings=DeferredSourceReviewSettings(mode="enforce"),
            now=now,
        )

    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == promoted.agent_id)
    )
    assert promoted.status == AgentStatus.ATH_PENDING_REVIEW
    assert updated.status == AgentStatus.SCORED
    assert review is not None and review.status == "pending"
    assert review.original_evidence["deferred_review"]["rank"] == 5
    assert review.original_evidence["deferred_review"]["triggers"] == ["top_five"]


def test_unranked_candidate_never_qualifies() -> None:
    candidate = _row(0, 0.99, eligible=False)
    decision = evaluate_deferred_review(
        agent_id=candidate.agent_id,
        ledger=[candidate],
        settings=DeferredSourceReviewSettings(mode="enforce"),
    )

    assert decision.triggered is False
    assert decision.rank is None
    assert decision.evidence == {"eligible": False}


@pytest.mark.asyncio
async def test_observe_appends_audit_even_with_existing_review(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="observe-miner",
        name="observe-agent",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
    )
    review = AthReview(
        review_id=uuid4(),
        agent_id=agent.agent_id,
        status="resolved",
        opened_at=now - timedelta(days=1),
        resolved_at=now - timedelta(hours=1),
        resolved_by="operator",
        resolution="clear",
        resolution_reason="prior review cleared",
        original_reason="prior copy review",
        original_policy_version=9,
        original_evidence={},
        algorithm_provenance={"review_kind": "copy"},
    )
    async with session.begin():
        session.add_all([agent, review])
    decision = DeferredReviewDecision(
        True,
        ("top_five",),
        3,
        {
            "eligible": True,
            "rank": 3,
            "cohort_size": 10,
            "peer_count": 9,
            "candidate": {
                "composite": 0.9,
                "tool_mean": 0.9,
                "memory_mean": 0.9,
            },
            "thresholds": None,
            "triggers": ["top_five"],
        },
    )

    async with session.begin():
        await _record_deferred_review_decision(
            session,
            agent=agent,
            decision=decision,
            mode="observe",
            screening_attempt=None,
            score_count=3,
            now=now,
        )

    audit = await session.scalar(
        select(ScoreAuditEntry).where(ScoreAuditEntry.agent_id == agent.agent_id)
    )
    assert audit is not None
    assert audit.event == "transform_audit"
    assert audit.payload["audit_kind"] == "deferred_source_review"
    assert audit.payload["enforced"] is False
    assert audit.payload["qualified"] is True
    assert audit.payload["trigger_kinds"] == ["top_five"]
    assert "decision" not in audit.payload
    assert "candidate" not in audit.payload
    assert "thresholds" not in audit.payload
    assert review.original_reason == "prior copy review"
    assert review.status == "resolved"


@pytest.mark.asyncio
async def test_enforce_reopen_rebinds_current_review_kind_and_preserves_history(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="enforce-miner",
        name="enforce-agent",
        sha256="cd" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
    )
    review = AthReview(
        review_id=uuid4(),
        agent_id=agent.agent_id,
        status="resolved",
        opened_at=now - timedelta(days=1),
        resolved_at=now - timedelta(hours=1),
        resolved_by="operator",
        resolution="clear",
        resolution_reason="prior review cleared",
        original_reason="prior copy review",
        original_policy_version=9,
        original_evidence={"copy_signal": "lexical"},
        algorithm_provenance={"review_kind": "copy", "algorithm_version": "old"},
    )
    async with session.begin():
        session.add_all([agent, review])
    decision = DeferredReviewDecision(
        True,
        ("top_five",),
        1,
        {
            "eligible": True,
            "rank": 1,
            "cohort_size": 10,
            "peer_count": 9,
            "candidate": {
                "composite": 0.95,
                "tool_mean": 0.95,
                "memory_mean": 0.95,
            },
            "thresholds": None,
            "triggers": ["top_five"],
        },
    )

    async with session.begin():
        await _record_deferred_review_decision(
            session,
            agent=agent,
            decision=decision,
            mode="enforce",
            screening_attempt=None,
            score_count=3,
            now=now,
        )

    action = await session.scalar(
        select(AthReviewAction).where(AthReviewAction.review_id == review.review_id)
    )
    assert review.status == "pending"
    assert review.algorithm_provenance["review_kind"] == "deferred_source_review"
    assert review.original_evidence["prior_review"]["original_reason"] == (
        "prior copy review"
    )
    assert review.original_evidence["prior_review"]["algorithm_provenance"] == {
        "review_kind": "copy",
        "algorithm_version": "old",
    }
    assert action is not None and action.action == "reopen"
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW


@pytest.mark.asyncio
async def test_enforce_reopen_of_copy_review_clears_the_matched_pointer(
    session: AsyncSession,
) -> None:
    """A reopened copy hold must not keep pointing at its matched agent.

    ``_record_deferred_review_decision`` clears ``agent.duplicate_of``, and
    ``resolve_copy_review`` refuses to resolve while that disagrees with
    ``review.original_duplicate_of``. Retaining the copy pointer therefore made
    the reopened review permanently unresolvable: every clear came back 409
    "agent hold evidence no longer matches review", and the agent stayed in
    ath_pending_review -- excluded from the emission ledger -- with no operator
    action able to release it.
    """
    now = datetime.now(UTC)
    matched = Agent(
        agent_id=uuid4(),
        miner_hotkey="matched-miner",
        name="matched-agent",
        sha256="ef" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
    )
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="reopen-miner",
        name="reopen-agent",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
        duplicate_of=matched.agent_id,
    )
    review = AthReview(
        review_id=uuid4(),
        agent_id=agent.agent_id,
        status="resolved",
        opened_at=now - timedelta(days=1),
        resolved_at=now - timedelta(hours=1),
        resolved_by="operator",
        resolution="clear",
        resolution_reason="copy hold cleared on provenance",
        original_reason="prior copy review",
        original_duplicate_of=matched.agent_id,
        original_policy_version=9,
        original_evidence={"copy_signal": "lexical"},
        algorithm_provenance={"review_kind": "copy", "algorithm_version": "old"},
    )
    async with session.begin():
        session.add_all([matched, agent, review])
    decision = DeferredReviewDecision(
        True,
        ("top_five",),
        1,
        {
            "eligible": True,
            "rank": 1,
            "cohort_size": 10,
            "peer_count": 9,
            "candidate": {"composite": 0.98, "tool_mean": 0.98, "memory_mean": 0.98},
            "thresholds": None,
            "triggers": ["top_five"],
        },
    )

    async with session.begin():
        await _record_deferred_review_decision(
            session,
            agent=agent,
            decision=decision,
            mode="enforce",
            screening_attempt=None,
            score_count=3,
            now=now,
        )

    # The agent and its review must agree, or resolve_copy_review 409s forever.
    assert agent.duplicate_of is None
    assert review.original_duplicate_of is None
    assert agent.duplicate_of == review.original_duplicate_of
    assert agent.review_reason == review.original_reason
    # The discarded pointer stays recoverable in the audit snapshot.
    assert review.original_evidence["prior_review"]["original_duplicate_of"] == str(
        matched.agent_id
    )


@pytest.mark.asyncio
async def test_terminal_deep_attempt_suppresses_old_admission_marker(
    session: AsyncSession,
) -> None:
    admitted_at = datetime.now(UTC) - timedelta(hours=2)
    opened_at = admitted_at + timedelta(hours=1)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="terminal-miner",
        name="terminal-agent",
        sha256="ef" * 32,
        status=AgentStatus.SCORED,
    )
    admission = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=agent.agent_id,
        screener_hotkey="screener",
        policy_version=9,
        status="passed",
        started_at=admitted_at,
        deadline=admitted_at + timedelta(minutes=30),
        finished_at=admitted_at + timedelta(minutes=5),
        reason_code="deferred-mechanical-admission",
        build_only=True,
    )
    review = AthReview(
        review_id=uuid4(),
        agent_id=agent.agent_id,
        status="resolved",
        opened_at=opened_at,
        resolved_at=opened_at + timedelta(minutes=30),
        resolved_by="operator",
        resolution="clear",
        resolution_reason="operator cleared terminal inconclusive evidence",
        original_reason="deferred review",
        original_policy_version=9,
        original_evidence={"previous_status": AgentStatus.SCORED.value},
        algorithm_provenance={"review_kind": "deferred_source_review"},
    )
    deep = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=agent.agent_id,
        screener_hotkey="screener",
        policy_version=9,
        status="passed",
        started_at=opened_at + timedelta(minutes=1),
        deadline=opened_at + timedelta(minutes=31),
        finished_at=opened_at + timedelta(minutes=10),
        build_only=False,
    )
    async with session.begin():
        session.add_all([agent, admission])
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent.agent_id,
                attempt_id=admission.attempt_id,
                screener_hotkey="screener",
                policy_version=9,
                manifest_digest="a" * 64,
                reason_code="source-review-inconclusive",
                status="resolved",
                resolved_at=admitted_at + timedelta(minutes=5),
                resolved_by="platform:deferred-source-review",
                resolution="rescreen",
                resolution_reason=(
                    "Deep source review deferred until score qualification"
                ),
            )
        )
        session.add_all([review, deep])

    assert await _deferred_screening_attempt(session, agent_id=agent.agent_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "bypass"])
async def test_off_and_bypass_compute_nothing_at_all(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """``off`` and ``bypass`` are true skips, not suppressed holds.

    ``observe`` already covers "qualify but do not hold". The distinction that
    makes these separate modes is that the qualification is never *computed*,
    so the canonical ledger is not even read. Asserting only the absence of a
    hold would pass in either mode and would not notice one quietly degrading
    into an unrecorded ``observe``.

    For ``bypass`` this is the post-score half of "no source review at all";
    ``test_bypass_admits_on_the_cheap_screen_like_enforce`` pins the pre-score
    half. Together they are the whole claim: nothing expensive runs at either
    end, and the submission goes straight to validator scoring.
    """
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=f"{mode}-miner",
        name=f"{mode}-agent",
        sha256="ef" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
    )
    admission = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=agent.agent_id,
        screener_hotkey="screener",
        policy_version=9,
        status="passed",
        started_at=now - timedelta(hours=1),
        deadline=now - timedelta(minutes=30),
        finished_at=now - timedelta(minutes=45),
        reason_code="deferred-mechanical-admission",
        build_only=True,
    )
    async with session.begin():
        session.add_all([agent, admission])

    async def _fail(*_args: object, **_kwargs: object) -> list[LedgerRow]:
        raise AssertionError(f"{mode} mode must not read the canonical ledger")

    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _fail
    )
    async with session.begin():
        await _evaluate_and_record_deferred_review(
            session,
            agent=agent,
            bench_version=8,
            score_count=3,
            settings=DeferredSourceReviewSettings(mode=mode),  # type: ignore[arg-type]
            now=now,
        )

    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    audit = await session.scalar(
        select(ScoreAuditEntry).where(ScoreAuditEntry.agent_id == agent.agent_id)
    )
    assert review is None
    assert audit is None
    assert agent.status == AgentStatus.SCORED
    assert agent.review_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "observe", "enforce", "bypass"])
async def test_copy_hold_survives_every_deferred_mode(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Turning the source-integrity branch down never releases a copy hold.

    This board scopes the *source-integrity* branch only. The dangerous misread
    is that ``mode="off"`` -- or, worse, the new ``mode="bypass"``, which really
    does mean "no source review at all" -- also stands down plagiarism
    enforcement, so pin the
    boundary directly: an agent held for copy review keeps its pending hold, its
    ``review_kind``, its matched pointer and its ``ATH_PENDING_REVIEW`` status in
    every mode. The deferred path acts only on ``SCORED``/``LIVE`` rows, so a
    copy-held agent is outside its reach by construction.
    """
    now = datetime.now(UTC)
    original = Agent(
        agent_id=uuid4(),
        miner_hotkey="original-miner",
        name="original-agent",
        sha256="a1" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
    )
    matched = original.agent_id
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="copy-miner",
        name="copy-agent",
        sha256="ba" * 32,
        status=AgentStatus.ATH_PENDING_REVIEW,
        duplicate_of=matched,
        review_reason="Near-duplicate of an earlier submission",
        screening_policy_version=9,
    )
    review = AthReview(
        review_id=uuid4(),
        agent_id=agent.agent_id,
        status="pending",
        opened_at=now - timedelta(hours=2),
        original_duplicate_of=matched,
        original_reason="Near-duplicate of an earlier submission",
        original_policy_version=9,
        original_evidence={"content_fingerprint_version": 3},
        algorithm_provenance={
            "snapshot": "score-finalization",
            "review_kind": "copy",
            "opened_at_source": "agent_finalized_audit",
        },
    )
    async with session.begin():
        session.add_all([original, agent, review])

    async def _fail(*_args: object, **_kwargs: object) -> list[LedgerRow]:
        raise AssertionError("a copy-held agent must never reach ledger evaluation")

    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _fail
    )
    async with session.begin():
        await _evaluate_and_record_deferred_review(
            session,
            agent=agent,
            bench_version=8,
            score_count=3,
            settings=DeferredSourceReviewSettings(mode=mode),  # type: ignore[arg-type]
            now=now,
        )

    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert agent.duplicate_of == matched
    assert review.status == "pending"
    assert review.resolution is None
    assert review.original_duplicate_of == matched
    assert review.algorithm_provenance["review_kind"] == "copy"


def test_double_check_counts_held_rows_so_holds_cannot_cascade() -> None:
    """Held rows vanish from the ledger but keep their slot.

    With #1-#5 held, the old #6 is ledger rank one. Without the held
    composites it would be held too, then #7, and so on down the board.
    """
    ledger = [_row(index, 0.80 - index / 100) for index in range(6)]
    held = [0.95, 0.94, 0.93, 0.92, 0.91]

    demoted = evaluate_integrity_double_check(
        agent_id=ledger[0].agent_id, ledger=ledger, held_composites=held
    )
    assert demoted.triggered is False
    assert demoted.rank == 6
    assert demoted.evidence["ledger_rank"] == 1
    assert demoted.evidence["held_above"] == 5

    # A genuinely better score still outranks the held rows.
    leader = _row(9, 0.99)
    promoted = evaluate_integrity_double_check(
        agent_id=leader.agent_id, ledger=[leader, *ledger], held_composites=held
    )
    assert promoted.triggered is True
    assert promoted.rank == 1
    assert promoted.triggers == ("top_five",)


def _scored(row: LedgerRow) -> Agent:
    return Agent(
        agent_id=row.agent_id,
        miner_hotkey=row.miner_hotkey,
        name=f"ranked-{row.agent_id.int}",
        sha256=row.sha256,
        status=AgentStatus.SCORED,
        screening_policy_version=SCREENING_POLICY_VERSION,
    )


@pytest.mark.asyncio
async def test_stale_review_rows_do_not_reserve_top_five_slots(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    statuses = (
        AgentStatus.REJECTED,
        AgentStatus.SCORED,
        AgentStatus.ATH_PENDING_REVIEW,
    )
    async with session.begin():
        for index, status in enumerate(statuses):
            agent = _scored(_row(index, 0.9))
            agent.status = status
            session.add(agent)
            session.add(
                AthReview(
                    review_id=uuid4(),
                    agent_id=agent.agent_id,
                    status="pending",
                    opened_at=now,
                    original_reason="historical deferred review",
                    original_policy_version=13,
                    original_evidence={
                        "deferred_review": {"candidate": {"composite": 0.9}}
                    },
                    algorithm_provenance={"review_kind": "deferred_source_review"},
                )
            )
    assert await _held_post_score_review_composites(session, bench_version=8) == [0.9]


@pytest.mark.asyncio
async def test_double_check_holds_fully_reviewed_top_five_once(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """eval -> top five -> integrity double-check, even after a full screen."""
    now = datetime.now(UTC)
    ledger = [_row(index, 0.90 - index / 100) for index in range(7)]
    agents = [_scored(row) for row in ledger]
    async with session.begin():
        session.add_all(agents)

    async def _ledger(*_args: object, **_kwargs: object) -> list[LedgerRow]:
        return ledger

    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _ledger
    )
    settings = DeferredSourceReviewSettings(integrity_double_check_mode="enforce")
    async with session.begin():
        await _evaluate_and_record_integrity_double_check(
            session, bench_version=8, settings=settings, now=now
        )

    assert [agent.status for agent in agents] == [
        *([AgentStatus.ATH_PENDING_REVIEW] * 5),
        AgentStatus.SCORED,
        AgentStatus.SCORED,
    ]
    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agents[0].agent_id)
    )
    assert review is not None and review.status == "pending"
    assert review.algorithm_provenance["review_kind"] == "deferred_source_review"
    assert review.algorithm_provenance["trigger"] == "integrity_double_check"
    assert review.original_evidence["deferred_review"]["bench_version"] == 8
    assert review.original_evidence["deferred_review"]["rank"] == 1
    assert agents[0].review_reason == review.original_reason
    markers = list(
        await session.scalars(
            select(ScoreAuditEntry).where(
                ScoreAuditEntry.agent_id == agents[0].agent_id
            )
        )
    )
    assert [entry.payload["audit_kind"] for entry in markers] == [
        "integrity_double_check"
    ]
    assert markers[0].payload["enforced"] is True
    await session.commit()

    # The next mutation reads a ledger without the five holds. Their snapshot
    # composites keep #6 and #7 out of the top five.
    async def _after(*_args: object, **_kwargs: object) -> list[LedgerRow]:
        return ledger[5:]

    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _after
    )
    async with session.begin():
        await _evaluate_and_record_integrity_double_check(
            session, bench_version=8, settings=settings, now=now
        )
    assert agents[5].status == AgentStatus.SCORED
    assert agents[6].status == AgentStatus.SCORED

    # A clean deep pass restores the row. It is never double-checked again,
    # even once its review row is later reopened for an unrelated copy hold.
    async with session.begin():
        review.status = "resolved"
        review.resolution = "clear"
        review.resolved_at = now
        review.resolved_by = "platform:deferred-source-review"
        review.resolution_reason = "Deferred source review completed cleanly"
        review.algorithm_provenance = {"review_kind": "copy"}
        agents[0].status = AgentStatus.SCORED
    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _ledger
    )
    async with session.begin():
        await _evaluate_and_record_integrity_double_check(
            session, bench_version=8, settings=settings, now=now
        )
    assert agents[0].status == AgentStatus.SCORED


@pytest.mark.asyncio
async def test_double_check_observe_records_once_and_holds_nothing(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    ledger = [_row(index, 0.90 - index / 100) for index in range(2)]
    agents = [_scored(row) for row in ledger]
    async with session.begin():
        session.add_all(agents)

    async def _ledger(*_args: object, **_kwargs: object) -> list[LedgerRow]:
        return ledger

    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _ledger
    )
    settings = DeferredSourceReviewSettings(integrity_double_check_mode="observe")
    for _mutation in range(2):
        async with session.begin():
            await _evaluate_and_record_integrity_double_check(
                session, bench_version=8, settings=settings, now=now
            )

    entries = list(
        await session.scalars(
            select(ScoreAuditEntry).where(
                ScoreAuditEntry.agent_id.in_([agent.agent_id for agent in agents])
            )
        )
    )
    assert sorted(entry.payload["enforced"] for entry in entries) == [False, False]
    assert all(agent.status == AgentStatus.SCORED for agent in agents)
    assert await session.scalar(select(AthReview)) is None


@pytest.mark.asyncio
async def test_double_check_off_reads_nothing(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(*_args: object, **_kwargs: object) -> list[LedgerRow]:
        raise AssertionError("off must not read the canonical ledger")

    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _fail
    )
    async with session.begin():
        await _evaluate_and_record_integrity_double_check(
            session,
            bench_version=8,
            settings=DeferredSourceReviewSettings(),
            now=datetime.now(UTC),
        )


# ── Public projection (#562) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["top_five"], ["top_five"]),
        (["tool_anomaly"], ["anomaly"]),
        (["memory_anomaly", "top_five", "composite_anomaly"], ["top_five", "anomaly"]),
        (["unknown-private-trigger"], []),
        ("top_five", []),
        (None, []),
    ],
)
def test_public_deferred_review_triggers_are_coarse(
    raw: object, expected: list[str]
) -> None:
    assert (
        public_deferred_review_triggers({"deferred_review": {"triggers": raw}})
        == expected
    )
    assert public_deferred_review_triggers(None) == []
    assert public_deferred_review_triggers({"deferred_review": "bad"}) == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("source-review-inconclusive", True),
        ("source-review-read-budget-exhausted", True),
        ("source-review-step-budget-exhausted", True),
        ("source-review-lease-budget-exhausted", True),
        ("l2-lease-budget-exhausted", True),
        ("l2-model-budget-exhausted", True),
        ("lease-budget-exhausted", True),
        ("l2-model-inconclusive", True),
        ("l2-model-total-budget", True),
        ("l2-model-tool-budget", True),
        ("l2-model-step-budget", True),
        ("l2-runtime-evidence-unavailable", True),
        # Exact set, not a suffix: an unknown budget code fails closed.
        ("source-review-finding-budget-exhausted", False),
        ("l3-critic-model-budget-exhausted", False),
        ("source-safety-malicious-risk", False),
        ("agentic-source-review-tripwire", False),
        ("adjudicated-source-review-escalate", False),
        ("some-future-finding", False),
        (None, False),
    ],
)
def test_no_finding_codes_are_an_exact_allowlist(
    code: str | None, expected: bool
) -> None:
    assert is_no_finding_reason_code(code) is expected


def _conclude(
    *,
    active: bool,
    evidence: object,
    quarantined: bool = False,
    code: str | None = None,
    quarantine_digest: str | None = None,
    quarantine_finding: object = None,
    quarantine_audit: object = None,
) -> object:
    return public_review_conclusion(
        deferred_review_active=active,
        deferred_evidence=evidence,
        quarantined=quarantined,
        screening_reason_code=code,
        quarantine_finding_digest=quarantine_digest,
        quarantine_finding=quarantine_finding,
        quarantine_review_audit=quarantine_audit,
    )


def _deep(**result: object) -> dict[str, object]:
    return {"deep_review_result": result}


# Audit shapes mirror the real producers in workers/screener: L1
# ``SourceReviewBudgetExhausted.audit()``, the L2 trajectory-budget audit, the L2
# ``l2-model-inconclusive`` audit, and the V13 ``_runtime_evidence_hold``
# preflight audit (lease unavailable / review disabled, zero steps).
L1_READ_BUDGET_AUDIT = ScreenReviewAudit(
    stage="l1",
    reason_code="source-review-read-budget-exhausted",
    prompt_revision="l1-v13",
    max_steps=240,
    steps_used=37,
    max_read_bytes=320_000,
    read_bytes_used=338_278,
).model_dump(mode="json")
L2_STEP_BUDGET_AUDIT = ScreenReviewAudit(
    stage="l2",
    reason_code="l2-model-step-budget",
    prompt_revision="l2-v13",
    harness_revision="h1",
    max_steps=64,
    steps_used=64,
    model_steps_observed=64,
    tool_calls_observed=80,
    budget_stop_reason="step",
).model_dump(mode="json")
L2_INCONCLUSIVE_AUDIT = ScreenReviewAudit(
    stage="l2",
    reason_code="l2-model-inconclusive",
    prompt_revision="l2-v13",
    harness_revision="h1",
    max_steps=64,
    steps_used=12,
    model_disposition="inconclusive",
    resolution_basis="insufficient_static_evidence",
    model_steps_observed=12,
    tool_calls_observed=20,
    budget_stop_reason="none",
).model_dump(mode="json")


def _preflight_audit(cause: str) -> dict[str, object]:
    return ScreenReviewAudit.model_validate(
        {
            "stage": "l2",
            "reason_code": "l2-runtime-evidence-unavailable",
            "prompt_revision": "l2-v13",
            "harness_revision": "h1",
            "max_steps": 64,
            "steps_used": 0,
            "max_input_tokens": 400_000,
            "input_tokens_used": 0,
            "max_output_tokens": 64_000,
            "output_tokens_used": 0,
            "max_cost_usd": 5.0,
            "cost_usd_used": 0,
            "model_steps_observed": 0,
            "tool_calls_observed": 0,
            "requested_model": "provider/model",
            "final_stage": "preflight",
            "cause_detail": cause,
            "max_elapsed_ms": 600_000,
            "elapsed_ms": 0,
        }
    ).model_dump(mode="json")


# (audit, expected) for a no-verdict hold with no finding, shared by the
# deferred and quarantine routes.
_AUDIT_CASES = [
    # No recorded audit: nothing proves a model review ran.
    (None, "not_reviewed"),
    # V13 signed-runtime preflight: stopped before any model review.
    (_preflight_audit("lease_unavailable"), "not_reviewed"),
    (_preflight_audit("review_disabled"), "not_reviewed"),
    # Malformed or empty audits prove nothing.
    ({"stage": "l3", "reason_code": "x"}, "not_reviewed"),
    ({}, "not_reviewed"),
    # A well-formed audit with no model steps is not a review either.
    (
        {**L2_INCONCLUSIVE_AUDIT, "steps_used": 0, "model_steps_observed": 0},
        "not_reviewed",
    ),
    # A recorded budget audit: the review ran and ran out of budget.
    (L1_READ_BUDGET_AUDIT, "budget_exhausted"),
    (L2_STEP_BUDGET_AUDIT, "budget_exhausted"),
    # The review ran, ended inconclusive, and exhausted nothing.
    (L2_INCONCLUSIVE_AUDIT, "no_finding"),
]


@pytest.mark.parametrize(("audit", "expected"), _AUDIT_CASES)
@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ("pass_inconclusive", "source-review-inconclusive"),
        ("inconclusive", "source-review-inconclusive"),
        ("inconclusive", "source-review-read-budget-exhausted"),
        ("inconclusive", "l2-model-step-budget"),
        ("inconclusive", "l2-model-inconclusive"),
        ("inconclusive", "l2-runtime-evidence-unavailable"),
    ],
)
def test_deferred_no_verdict_requires_a_recorded_review_audit(
    outcome: str, code: str, audit: object, expected: str
) -> None:
    evidence = _deep(outcome=outcome, reason_code=code, review_audit=audit)
    assert _conclude(active=True, evidence=evidence) == expected


@pytest.mark.parametrize(("audit", "expected"), _AUDIT_CASES)
@pytest.mark.parametrize(
    "code",
    [
        "source-review-inconclusive",
        "source-review-step-budget-exhausted",
        "l2-model-total-budget",
        "l2-model-inconclusive",
        "l2-runtime-evidence-unavailable",
    ],
)
def test_quarantine_no_verdict_requires_a_recorded_review_audit(
    code: str, audit: object, expected: str
) -> None:
    assert (
        _conclude(
            active=False,
            evidence=None,
            quarantined=True,
            code=code,
            quarantine_audit=audit,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        # A recorded finding is never softened, whatever the code or audit says.
        (
            {
                "outcome": "inconclusive",
                "reason_code": "source-review-inconclusive",
                "finding_digest": "ab" * 32,
                "review_audit": L1_READ_BUDGET_AUDIT,
            },
            "adverse_signal",
        ),
        (
            {"outcome": "quarantine", "reason_code": "source-safety-malicious-risk"},
            "adverse_signal",
        ),
        # A budget audit cannot soften an adverse or unknown code.
        (
            {
                "outcome": "quarantine",
                "reason_code": "agentic-source-review-tripwire",
                "review_audit": L1_READ_BUDGET_AUDIT,
            },
            "adverse_signal",
        ),
        # Interrupted attempts have no conclusion yet.
        (
            {
                "outcome": "retryable_infra",
                "reason_code": "docker-build-infrastructure",
            },
            "pending",
        ),
        (
            {"outcome": "retryable_infra", "reason_code": "l2-http-503-provider"},
            "pending",
        ),
        (
            {"outcome": "deterministic_reject", "reason_code": "health-contract"},
            "pending",
        ),
        # ...unless the interrupted attempt still carries a finding.
        (
            {
                "outcome": "retryable_infra",
                "reason_code": "docker-build-infrastructure",
                "finding_digest": "ab" * 32,
            },
            "adverse_signal",
        ),
        # Any other deterministic reject, unknown outcomes and unknown codes
        # fail closed.
        (
            {"outcome": "deterministic_reject", "reason_code": "archive-invalid"},
            "adverse_signal",
        ),
        (
            {"outcome": "future-outcome", "reason_code": "some-future-code"},
            "adverse_signal",
        ),
        ({"outcome": "inconclusive"}, "adverse_signal"),
    ],
)
def test_deep_review_conclusion(result: dict[str, object], expected: str) -> None:
    assert _conclude(active=True, evidence=_deep(**result)) == expected


def test_public_review_conclusion_without_a_deep_result() -> None:
    # The admission-time code on the deferred snapshot is not a deep result.
    assert (
        _conclude(
            active=True,
            evidence={
                "deferred_review": {
                    "screening_reason_code": "source-review-inconclusive"
                }
            },
        )
        == "pending"
    )
    assert (
        _conclude(
            active=True,
            evidence={},
            quarantined=True,
            code="source-review-read-budget-exhausted",
            quarantine_audit=L1_READ_BUDGET_AUDIT,
        )
        == "budget_exhausted"
    )
    assert (
        _conclude(active=False, evidence=None, quarantined=True, code="tripwire")
        == "adverse_signal"
    )
    assert _conclude(active=False, evidence=None, quarantined=True) is None
    assert (
        _conclude(active=False, evidence=None, code="source-review-inconclusive")
        is None
    )


@pytest.mark.parametrize(
    ("digest", "finding"),
    [("cd" * 32, None), (None, {"risk": "high"}), ("cd" * 32, {"risk": "high"})],
)
def test_quarantine_finding_is_never_softened(digest: object, finding: object) -> None:
    for code in ("source-review-inconclusive", "l2-model-budget-exhausted", None):
        assert (
            _conclude(
                active=False,
                evidence=None,
                quarantined=True,
                code=code,
                quarantine_digest=digest if isinstance(digest, str) else None,
                quarantine_finding=finding,
                quarantine_audit=L1_READ_BUDGET_AUDIT,
            )
            == "adverse_signal"
        )


def test_unknown_budget_code_on_quarantine_is_adverse() -> None:
    assert (
        _conclude(
            active=False,
            evidence=None,
            quarantined=True,
            code="source-review-finding-budget-exhausted",
            quarantine_audit=L1_READ_BUDGET_AUDIT,
        )
        == "adverse_signal"
    )
