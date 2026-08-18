"""Out-of-band composite escalation to ATH review (issue #476).

Two layers, mirroring ``test_transform_audit.py`` (pure verdict) and
``test_deferred_source_review.py`` (DB hold recording):

* the pure :func:`evaluate_score_outlier` statistic and its edge cases, and
* the :func:`_evaluate_and_record_outlier_escalation` helper that reuses the
  established ATH hold mechanism against a real Postgres session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.endpoints import validator as v
from ditto.api_server.endpoints.validator import (
    _evaluate_and_record_outlier_escalation,
    _outlier_escalation_settings_from_env,
)
from ditto.api_server.outlier_escalation import (
    OUTLIER_REVIEW_KIND,
    OUTLIER_REVIEW_REASON,
    OutlierEscalationSettings,
    evaluate_score_outlier,
)
from ditto.db.models import Agent, AthReview, ScoreAuditEntry

# A cohort with real spread (median 0.60, MAD 0.01), for the modified-z path.
_SPREAD_COHORT = [0.58, 0.59, 0.60, 0.61, 0.62, 0.60, 0.60, 0.61]


def _enforce(**overrides: object) -> OutlierEscalationSettings:
    base = {
        "mode": "enforce",
        "min_bench_version": 12,
        "min_cohort_size": 8,
        "modified_z_threshold": 6.0,
        "min_composite_floor": 0.90,
    }
    base.update(overrides)
    return OutlierEscalationSettings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure statistic
# ---------------------------------------------------------------------------


def test_out_of_band_high_composite_is_held() -> None:
    decision = evaluate_score_outlier(
        composite=0.99, cohort=_SPREAD_COHORT, settings=_enforce()
    )
    assert decision.held is True
    assert decision.reason == OUTLIER_REVIEW_REASON
    assert decision.evidence["cohort_median"] == pytest.approx(0.60, abs=1e-9)
    assert decision.evidence["cohort_mad"] == pytest.approx(0.01, abs=1e-9)
    assert decision.evidence["above_floor"] is True
    # The neutral reason carries no cohort numbers; those live in evidence only.
    assert "0.60" not in (decision.reason or "")


def test_in_band_composite_is_not_held() -> None:
    decision = evaluate_score_outlier(
        composite=0.615, cohort=_SPREAD_COHORT, settings=_enforce()
    )
    assert decision.held is False
    assert decision.reason is None
    # Below the modified-z threshold: a normal member of a tight cohort.
    assert cast(float, decision.evidence["modified_z"]) < 6.0


def test_small_cohort_does_not_hold() -> None:
    decision = evaluate_score_outlier(
        composite=0.99, cohort=[0.50, 0.51, 0.52], settings=_enforce()
    )
    assert decision.held is False
    assert decision.evidence["anomaly_unavailable"] == "cohort_too_small"
    # Fails closed: no median/MAD invented from three points.
    assert "cohort_median" not in decision.evidence


def test_far_outlier_below_floor_is_not_held() -> None:
    """A spike far in MAD terms but nowhere near ranks-threatening never holds.

    The absolute floor is what keeps a statistically-odd but mediocre row out of
    the queue -- only UPWARD spikes near the top of the scale escalate.
    """
    low_cohort = [0.08, 0.09, 0.10, 0.11, 0.12, 0.10, 0.10, 0.11]
    decision = evaluate_score_outlier(
        composite=0.30, cohort=low_cohort, settings=_enforce()
    )
    assert cast(float, decision.evidence["modified_z"]) > 6.0
    assert decision.evidence["above_floor"] is False
    assert decision.held is False


def test_downward_outlier_is_not_held() -> None:
    decision = evaluate_score_outlier(
        composite=0.01, cohort=_SPREAD_COHORT, settings=_enforce()
    )
    assert decision.evidence["upward"] is False
    assert decision.held is False


def test_degenerate_cohort_holds_upward_spike_without_div_by_zero() -> None:
    """MAD=0 (identical cohort) must not divide by zero and must still catch a
    ranks-threatening spike above the floor."""
    flat = [0.50] * 10
    held = evaluate_score_outlier(composite=0.99, cohort=flat, settings=_enforce())
    assert held.held is True
    assert held.evidence["cohort_mad"] == 0.0
    assert held.evidence["modified_z"] is None

    # Same degenerate cohort, candidate below the floor: not held.
    below = evaluate_score_outlier(composite=0.55, cohort=flat, settings=_enforce())
    assert below.held is False
    assert below.evidence["modified_z"] is None

    # Candidate equal to the cohort centre: not an upward deviation.
    equal = evaluate_score_outlier(composite=0.50, cohort=flat, settings=_enforce())
    assert equal.held is False


def test_settings_default_off_and_env_override() -> None:
    assert OutlierEscalationSettings().mode == "off"
    # Ships disabled: an unset environment yields the no-op default.
    settings = _outlier_escalation_settings_from_env()
    assert settings.mode == "off"
    assert v.OUTLIER_ESCALATION_SETTINGS.mode == "off"


def test_env_builder_parses_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DITTO_OUTLIER_ESCALATION_MODE", "enforce")
    monkeypatch.setenv("DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE", "12")
    monkeypatch.setenv("DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD", "not-a-number")
    settings = _outlier_escalation_settings_from_env()
    assert settings.mode == "enforce"
    assert settings.min_cohort_size == 12
    # Unparseable value degrades to the shipped default rather than crashing.
    assert (
        settings.modified_z_threshold
        == OutlierEscalationSettings().modified_z_threshold
    )
    monkeypatch.setenv("DITTO_OUTLIER_ESCALATION_MODE", "nonsense")
    assert _outlier_escalation_settings_from_env().mode == "off"


# ---------------------------------------------------------------------------
# DB hold recording
# ---------------------------------------------------------------------------


def _agent(status: AgentStatus = AgentStatus.SCORED) -> Agent:
    return Agent(
        agent_id=uuid4(),
        miner_hotkey=f"miner-{uuid4().hex[:8]}",
        name="outlier-agent",
        sha256="ab" * 32,
        status=status,
        screening_policy_version=9,
    )


@pytest.mark.asyncio
async def test_enforce_holds_out_of_band_v12_score(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    agent = _agent()
    async with session.begin():
        session.add(agent)

    async with session.begin():
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=12,
            composite=0.99,
            cohort=_SPREAD_COHORT,
            settings=_enforce(),
            now=now,
        )

    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    audit = await session.scalar(
        select(ScoreAuditEntry).where(ScoreAuditEntry.agent_id == agent.agent_id)
    )
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert agent.review_reason == OUTLIER_REVIEW_REASON
    assert review is not None and review.status == "pending"
    assert review.original_reason == OUTLIER_REVIEW_REASON
    assert review.algorithm_provenance["review_kind"] == OUTLIER_REVIEW_KIND
    assert review.algorithm_provenance["opened_at_source"] == "outlier_escalation"
    # Cohort statistics are recorded on the operator-only evidence snapshot.
    assert review.original_evidence["composite"] == pytest.approx(0.99)
    assert review.original_evidence["cohort_median"] == pytest.approx(0.60, abs=1e-9)
    assert review.original_evidence["cohort_size"] == len(_SPREAD_COHORT)
    assert audit is not None
    assert audit.payload["audit_kind"] == OUTLIER_REVIEW_KIND
    assert audit.payload["enforced"] is True


@pytest.mark.asyncio
async def test_in_band_v12_score_ranks_normally(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    agent = _agent()
    async with session.begin():
        session.add(agent)

    async with session.begin():
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=12,
            composite=0.615,
            cohort=_SPREAD_COHORT,
            settings=_enforce(),
            now=now,
        )

    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    assert agent.status == AgentStatus.SCORED
    assert agent.review_reason is None
    assert review is None


@pytest.mark.asyncio
async def test_v11_score_is_unaffected(session: AsyncSession) -> None:
    """Below the bench-version floor the gate is a no-op, even on a spike that
    would hold at v12. v8-v11 behaviour is untouched."""
    now = datetime.now(UTC)
    agent = _agent()
    async with session.begin():
        session.add(agent)

    async with session.begin():
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=11,
            composite=0.99,
            cohort=_SPREAD_COHORT,
            settings=_enforce(),
            now=now,
        )

    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    audit = await session.scalar(
        select(ScoreAuditEntry).where(ScoreAuditEntry.agent_id == agent.agent_id)
    )
    assert agent.status == AgentStatus.SCORED
    assert review is None
    assert audit is None


@pytest.mark.asyncio
async def test_small_cohort_v12_does_not_hold(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    agent = _agent()
    async with session.begin():
        session.add(agent)

    async with session.begin():
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=12,
            composite=0.99,
            cohort=[0.50, 0.51, 0.52],
            settings=_enforce(),
            now=now,
        )

    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    assert agent.status == AgentStatus.SCORED
    assert review is None


@pytest.mark.asyncio
async def test_observe_records_without_holding(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    agent = _agent()
    async with session.begin():
        session.add(agent)

    async with session.begin():
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=12,
            composite=0.99,
            cohort=_SPREAD_COHORT,
            settings=_enforce(mode="observe"),
            now=now,
        )

    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    audit = await session.scalar(
        select(ScoreAuditEntry).where(ScoreAuditEntry.agent_id == agent.agent_id)
    )
    assert agent.status == AgentStatus.SCORED
    assert agent.review_reason is None
    assert review is None
    assert audit is not None
    assert audit.payload["audit_kind"] == OUTLIER_REVIEW_KIND
    assert audit.payload["enforced"] is False


@pytest.mark.asyncio
async def test_off_mode_computes_nothing(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    agent = _agent()
    async with session.begin():
        session.add(agent)

    async with session.begin():
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=12,
            composite=0.99,
            cohort=_SPREAD_COHORT,
            settings=_enforce(mode="off"),
            now=now,
        )

    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    audit = await session.scalar(
        select(ScoreAuditEntry).where(ScoreAuditEntry.agent_id == agent.agent_id)
    )
    assert agent.status == AgentStatus.SCORED
    assert review is None
    assert audit is None


@pytest.mark.asyncio
async def test_existing_pending_review_is_not_duplicated(
    session: AsyncSession,
) -> None:
    """Idempotency: an agent already carrying a pending review is never given a
    second one (the unique agent_id row would raise), and its status is left as
    the prior hold set it."""
    now = datetime.now(UTC)
    agent = _agent(status=AgentStatus.ATH_PENDING_REVIEW)
    prior = AthReview(
        review_id=uuid4(),
        agent_id=agent.agent_id,
        status="pending",
        opened_at=now - timedelta(hours=1),
        original_reason="prior copy review",
        original_policy_version=9,
        original_evidence={},
        algorithm_provenance={"review_kind": "copy"},
    )
    async with session.begin():
        session.add_all([agent, prior])

    async with session.begin():
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=12,
            composite=0.99,
            cohort=_SPREAD_COHORT,
            settings=_enforce(),
            now=now,
        )

    reviews = (
        await session.scalars(
            select(AthReview).where(AthReview.agent_id == agent.agent_id)
        )
    ).all()
    # The status guard (only SCORED agents escalate) short-circuits before the
    # unique row is ever at risk, so the prior copy review is untouched.
    assert len(reviews) == 1
    assert reviews[0].algorithm_provenance["review_kind"] == "copy"
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
