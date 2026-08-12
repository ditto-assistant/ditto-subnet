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
)
from ditto.api_server.endpoints.validator import (
    _deferred_screening_attempt,
    _evaluate_and_record_deferred_review,
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
        screening_policy_version=9,
    )
    updated = Agent(
        agent_id=ledger[5].agent_id,
        miner_hotkey=ledger[5].miner_hotkey,
        name="updated-row",
        sha256=ledger[5].sha256,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
    )
    admission = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=promoted.agent_id,
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
async def test_off_mode_computes_nothing_at_all(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``off`` is a true skip, not a suppressed hold.

    ``observe`` already covers "qualify but do not hold". The distinction that
    makes ``off`` a separate mode is that the qualification is never *computed*,
    so the canonical ledger is not even read. Asserting only the absence of a
    hold would pass in either mode and would not notice ``off`` quietly
    degrading into an unrecorded ``observe``.
    """
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="off-miner",
        name="off-agent",
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
        raise AssertionError("off mode must not read the canonical ledger")

    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.list_eligible_ledger", _fail
    )
    async with session.begin():
        await _evaluate_and_record_deferred_review(
            session,
            agent=agent,
            bench_version=8,
            score_count=3,
            settings=DeferredSourceReviewSettings(mode="off"),
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
@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
async def test_copy_hold_survives_every_deferred_mode(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Turning the source-integrity branch down never releases a copy hold.

    This board scopes the *source-integrity* branch only. The dangerous misread
    is that ``mode="off"`` also stands down plagiarism enforcement, so pin the
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
