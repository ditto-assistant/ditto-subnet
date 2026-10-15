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

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.endpoints import validator as v
from ditto.api_server.endpoints.validator import (
    _evaluate_and_record_outlier_escalation,
    _outlier_escalation_settings_from_env,
)
from ditto.api_server.outlier_escalation import (
    OUTLIER_ALGORITHM_VERSION,
    OUTLIER_AXIS_EVIDENCE_KIND,
    OUTLIER_REVIEW_KIND,
    OUTLIER_REVIEW_REASON,
    AxisObservation,
    OutlierEscalationSettings,
    evaluate_score_outlier,
    load_outlier_escalation_settings,
    public_audit_evidence,
)
from ditto.db.models import Agent, AthReview, Score, ScoreAuditEntry
from ditto.db.queries.audit import EVENT_AUDIT

# A cohort with real spread (median 0.60, MAD 0.01), for the modified-z path.
_SPREAD_COHORT = [0.58, 0.59, 0.60, 0.61, 0.62, 0.60, 0.60, 0.61]
# The same spread shifted to 0.50 (median 0.50, MAD 0.01): an axis cohort.
_AXIS_COHORT = [round(value - 0.10, 2) for value in _SPREAD_COHORT]
# A composite cohort sitting just above the 0.90 floor (median 0.91, MAD 0.01),
# so an in-band composite can still be ranks-threatening.
_HIGH_COHORT = [round(value + 0.31, 2) for value in _SPREAD_COHORT]


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


# ---------------------------------------------------------------------------
# Per-axis evidence (outlier-escalation-v2)
# ---------------------------------------------------------------------------

_PER_AXIS_KEYS = ("per_axis", "per_axis_outlier_axes", "per_axis_enforce", "trigger")
# What the public audit chain records for a memory-only outlier.
_NEUTRAL_AXES = [
    {"axis": "tool_mean", "outlier": False},
    {"axis": "memory_mean", "outlier": True},
]


def _axes(
    tool: float,
    memory: float,
    *,
    tool_cohort: list[float] | None = None,
    memory_cohort: list[float] | None = None,
) -> dict[str, AxisObservation]:
    return {
        "tool_mean": AxisObservation(
            value=tool,
            cohort=_SPREAD_COHORT if tool_cohort is None else tool_cohort,
        ),
        "memory_mean": AxisObservation(
            value=memory,
            cohort=_AXIS_COHORT if memory_cohort is None else memory_cohort,
        ),
    }


def _axis(decision_evidence: dict[str, object], name: str) -> dict[str, object]:
    entries = cast(list[dict[str, object]], decision_evidence["per_axis"])
    [entry] = [entry for entry in entries if entry["axis"] == name]
    return entry


def test_single_axis_outlier_is_evidence_only_by_default() -> None:
    """Memory far out of band while the composite is in band: recorded, not held."""
    decision = evaluate_score_outlier(
        composite=0.615,
        cohort=_SPREAD_COHORT,
        settings=_enforce(),
        axes=_axes(0.61, 0.95),
    )
    assert decision.held is False
    assert decision.reason is None
    assert decision.axis_outliers == ("memory_mean",)
    evidence = decision.evidence
    assert evidence["algorithm_version"] == OUTLIER_ALGORITHM_VERSION
    assert evidence["per_axis_outlier_axes"] == ["memory_mean"]
    assert evidence["per_axis_enforce"] is False
    assert evidence["trigger"] is None
    memory = _axis(evidence, "memory_mean")
    assert memory["value"] == pytest.approx(0.95)
    assert memory["cohort_size"] == len(_AXIS_COHORT)
    assert memory["cohort_median"] == pytest.approx(0.50, abs=1e-9)
    assert memory["cohort_mad"] == pytest.approx(0.01, abs=1e-9)
    assert cast(float, memory["modified_z"]) > 6.0
    assert memory["upward"] is True
    assert memory["outlier"] is True
    assert memory["anomaly_unavailable"] is None
    tool = _axis(evidence, "tool_mean")
    assert tool["outlier"] is False
    assert cast(float, tool["modified_z"]) < 6.0


@pytest.mark.parametrize(
    ("composite", "cohort"),
    [
        (0.99, _SPREAD_COHORT),
        (0.615, _SPREAD_COHORT),
        (0.01, _SPREAD_COHORT),
        (0.30, [0.08, 0.09, 0.10, 0.11, 0.12, 0.10, 0.10, 0.11]),
        (0.99, [0.50] * 10),
        (0.55, [0.50] * 10),
        (0.99, [0.50, 0.51, 0.52]),
    ],
)
def test_composite_verdict_and_evidence_are_unchanged_by_axes(
    composite: float, cohort: list[float]
) -> None:
    """Per-axis evidence only ADDS keys: the composite verdict, reason and every
    composite evidence value are identical with or without axes, even when an
    axis is wildly out of band."""
    bare = evaluate_score_outlier(
        composite=composite, cohort=cohort, settings=_enforce()
    )
    with_axes = evaluate_score_outlier(
        composite=composite,
        cohort=cohort,
        settings=_enforce(),
        axes=_axes(0.99, 0.99),
    )
    assert with_axes.held is bare.held
    assert with_axes.reason == bare.reason
    assert bare.axis_outliers == ()
    assert not any(key in bare.evidence for key in _PER_AXIS_KEYS)
    stripped = {
        key: value
        for key, value in with_axes.evidence.items()
        if key not in _PER_AXIS_KEYS
    }
    assert stripped == bare.evidence
    assert list(stripped) == list(bare.evidence)


def test_composite_hold_carries_per_axis_evidence_and_trigger() -> None:
    decision = evaluate_score_outlier(
        composite=0.99,
        cohort=_SPREAD_COHORT,
        settings=_enforce(),
        axes=_axes(0.99, 0.99),
    )
    assert decision.held is True
    assert decision.evidence["trigger"] == "composite"
    assert decision.axis_outliers == ("tool_mean", "memory_mean")
    assert [entry["axis"] for entry in cast(list, decision.evidence["per_axis"])] == [
        "tool_mean",
        "memory_mean",
    ]


def test_per_axis_enforce_holds_single_axis_outlier_above_the_floor() -> None:
    """Opt-in: a single-axis outlier holds when the composite is in band but at
    or above the floor. Off (the default), the same row is evidence only."""
    axes = _axes(0.91, 0.95, tool_cohort=_HIGH_COHORT)
    off = evaluate_score_outlier(
        composite=0.92, cohort=_HIGH_COHORT, settings=_enforce(), axes=axes
    )
    assert off.held is False
    assert off.evidence["above_floor"] is True
    assert cast(float, off.evidence["modified_z"]) < 6.0
    assert off.axis_outliers == ("memory_mean",)

    on = evaluate_score_outlier(
        composite=0.92,
        cohort=_HIGH_COHORT,
        settings=_enforce(per_axis_enforce=True),
        axes=axes,
    )
    assert on.held is True
    assert on.reason == OUTLIER_REVIEW_REASON
    assert on.evidence["trigger"] == "per_axis"
    assert on.evidence["per_axis_enforce"] is True
    # The neutral reason never names the axis or its statistics.
    assert "memory" not in (on.reason or "")


def test_per_axis_enforce_never_holds_below_the_composite_floor() -> None:
    decision = evaluate_score_outlier(
        composite=0.615,
        cohort=_SPREAD_COHORT,
        settings=_enforce(per_axis_enforce=True),
        axes=_axes(0.61, 0.95),
    )
    assert decision.evidence["above_floor"] is False
    assert decision.axis_outliers == ("memory_mean",)
    assert decision.held is False
    assert decision.evidence["trigger"] is None


def test_small_axis_cohort_fails_closed() -> None:
    decision = evaluate_score_outlier(
        composite=0.615,
        cohort=_SPREAD_COHORT,
        settings=_enforce(per_axis_enforce=True),
        axes=_axes(0.99, 0.99, tool_cohort=[0.5, 0.51], memory_cohort=[0.5]),
    )
    assert decision.held is False
    assert decision.axis_outliers == ()
    for name in ("tool_mean", "memory_mean"):
        entry = _axis(decision.evidence, name)
        assert entry["anomaly_unavailable"] == "cohort_too_small"
        assert entry["outlier"] is False
        assert entry["cohort_median"] is None
        assert entry["modified_z"] is None


def test_small_composite_cohort_never_holds_on_an_axis() -> None:
    """Fail closed: a thin composite cohort records the condition and holds
    nothing, whatever a (larger) axis cohort says."""
    decision = evaluate_score_outlier(
        composite=0.99,
        cohort=[0.90, 0.91, 0.92],
        settings=_enforce(per_axis_enforce=True),
        axes=_axes(0.99, 0.99),
    )
    assert decision.evidence["anomaly_unavailable"] == "cohort_too_small"
    assert decision.axis_outliers == ("tool_mean", "memory_mean")
    assert decision.held is False
    assert decision.evidence["trigger"] is None


def test_downward_and_degenerate_axis_cohorts() -> None:
    decision = evaluate_score_outlier(
        composite=0.615,
        cohort=_SPREAD_COHORT,
        settings=_enforce(),
        axes=_axes(0.01, 0.51, memory_cohort=[0.50] * 10),
    )
    tool = _axis(decision.evidence, "tool_mean")
    assert tool["upward"] is False
    assert tool["outlier"] is False
    # Zero-MAD axis cohort: no division by zero, any upward value is out of band.
    memory = _axis(decision.evidence, "memory_mean")
    assert memory["cohort_mad"] == 0.0
    assert memory["modified_z"] is None
    assert memory["outlier"] is True
    assert decision.axis_outliers == ("memory_mean",)


@pytest.mark.parametrize(
    ("raw", "expected", "source"),
    [
        (None, False, "default"),
        ("true", True, "env"),
        (" ON ", True, "env"),
        ("1", True, "env"),
        ("false", False, "env"),
        ("off", False, "env"),
        ("", False, "default_invalid_env"),
        ("enforce", False, "default_invalid_env"),
    ],
)
def test_per_axis_enforce_env_parsing(
    raw: str | None, expected: bool, source: str
) -> None:
    environ = {} if raw is None else {"DITTO_OUTLIER_ESCALATION_PER_AXIS_ENFORCE": raw}
    loaded = load_outlier_escalation_settings(environ)
    assert loaded.settings.per_axis_enforce is expected
    assert loaded.sources.per_axis_enforce == source
    assert OutlierEscalationSettings().per_axis_enforce is False


async def _run(
    session: AsyncSession,
    *,
    composite: float,
    cohort: list[float],
    settings: OutlierEscalationSettings,
    axes: dict[str, AxisObservation],
) -> tuple[Agent, AthReview | None, list[ScoreAuditEntry]]:
    agent = _agent()
    async with session.begin():
        session.add(agent)
    async with session.begin():
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=12,
            composite=composite,
            cohort=cohort,
            settings=settings,
            now=datetime.now(UTC),
            axes=axes,
        )
    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    audits = list(
        (
            await session.scalars(
                select(ScoreAuditEntry).where(
                    ScoreAuditEntry.agent_id == agent.agent_id
                )
            )
        ).all()
    )
    return agent, review, audits


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["observe", "enforce"])
async def test_single_axis_outlier_records_evidence_without_holding(
    session: AsyncSession, mode: str
) -> None:
    agent, review, audits = await _run(
        session,
        composite=0.615,
        cohort=_SPREAD_COHORT,
        settings=_enforce(mode=mode),
        axes=_axes(0.61, 0.95),
    )
    assert agent.status == AgentStatus.SCORED
    assert agent.review_reason is None
    assert review is None
    [audit] = audits
    # Its own kind: never counted as a would-be hold or a hold.
    assert audit.payload["audit_kind"] == OUTLIER_AXIS_EVIDENCE_KIND
    assert audit.payload["enforced"] is False
    assert audit.payload["qualified"] is False
    assert audit.payload["bench_version"] == 12
    evidence = audit.payload["evidence"]
    # The chain is public: per-axis material is only the axis and its flag.
    assert evidence["per_axis"] == _NEUTRAL_AXES
    assert "per_axis_outlier_axes" not in evidence
    assert "per_axis_enforce" not in evidence
    assert evidence["trigger"] is None
    assert evidence["algorithm_version"] == OUTLIER_ALGORITHM_VERSION


@pytest.mark.asyncio
async def test_in_band_axes_and_composite_record_nothing(
    session: AsyncSession,
) -> None:
    agent, review, audits = await _run(
        session,
        composite=0.615,
        cohort=_SPREAD_COHORT,
        settings=_enforce(),
        axes=_axes(0.61, 0.51),
    )
    assert agent.status == AgentStatus.SCORED
    assert review is None
    assert audits == []


@pytest.mark.asyncio
async def test_per_axis_enforce_opens_the_normal_ath_hold(
    session: AsyncSession,
) -> None:
    agent, review, audits = await _run(
        session,
        composite=0.92,
        cohort=_HIGH_COHORT,
        settings=_enforce(per_axis_enforce=True),
        axes=_axes(0.91, 0.95, tool_cohort=_HIGH_COHORT),
    )
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert agent.review_reason == OUTLIER_REVIEW_REASON
    assert review is not None and review.status == "pending"
    assert review.algorithm_provenance["review_kind"] == OUTLIER_REVIEW_KIND
    assert review.algorithm_provenance["algorithm_version"] == OUTLIER_ALGORITHM_VERSION
    assert review.original_evidence["trigger"] == "per_axis"
    assert review.original_evidence["per_axis_outlier_axes"] == ["memory_mean"]
    [audit] = audits
    assert audit.payload["audit_kind"] == OUTLIER_REVIEW_KIND
    assert audit.payload["enforced"] is True


@pytest.mark.asyncio
async def test_per_axis_enforce_in_observe_mode_only_records_a_would_be_hold(
    session: AsyncSession,
) -> None:
    agent, review, audits = await _run(
        session,
        composite=0.92,
        cohort=_HIGH_COHORT,
        settings=_enforce(mode="observe", per_axis_enforce=True),
        axes=_axes(0.91, 0.95, tool_cohort=_HIGH_COHORT),
    )
    assert agent.status == AgentStatus.SCORED
    assert review is None
    [audit] = audits
    assert audit.payload["audit_kind"] == OUTLIER_REVIEW_KIND
    assert audit.payload["enforced"] is False
    assert audit.payload["evidence"]["trigger"] == "per_axis"


@pytest.mark.asyncio
async def test_composite_hold_snapshot_includes_per_axis_evidence(
    session: AsyncSession,
) -> None:
    agent, review, audits = await _run(
        session,
        composite=0.99,
        cohort=_SPREAD_COHORT,
        settings=_enforce(),
        axes=_axes(0.61, 0.95),
    )
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert review is not None
    assert review.original_evidence["trigger"] == "composite"
    assert review.original_evidence["per_axis_outlier_axes"] == ["memory_mean"]
    # The private review snapshot keeps the full per-axis statistics ...
    private_memory = _axis(review.original_evidence, "memory_mean")
    assert private_memory["cohort_median"] == pytest.approx(0.50, abs=1e-9)
    assert cast(float, private_memory["modified_z"]) > 6.0
    # One entry: the hold, not a second evidence-only axis entry.
    [audit] = audits
    assert audit.payload["audit_kind"] == OUTLIER_REVIEW_KIND
    # ... while the public chain gets the neutral projection, and the composite
    # fields exactly as before.
    public = audit.payload["evidence"]
    assert public["per_axis"] == _NEUTRAL_AXES
    assert {
        key: value for key, value in public.items() if key not in _PER_AXIS_KEYS
    } == {
        key: value
        for key, value in review.original_evidence.items()
        if key not in _PER_AXIS_KEYS
    }


@pytest.mark.asyncio
async def test_finalization_records_single_axis_evidence_end_to_end(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real score endpoint feeds each axis's quorum median and the eligible
    ledger's axes into the gate: an in-band composite with the memory axis far
    out of band finalizes SCORED and leaves one evidence-only axis entry."""
    from ditto.tests.api_server.endpoints.test_validator import (
        _BENCH_VERSION,
        _install_chain,
        _install_db,
        _score_to_quorum,
        _seed_agent,
    )

    monkeypatch.setattr(
        v,
        "OUTLIER_ESCALATION_SETTINGS",
        OutlierEscalationSettings(mode="observe", min_bench_version=_BENCH_VERSION),
    )
    for composite, peer_memory in zip(_SPREAD_COHORT, _AXIS_COHORT, strict=True):
        peer = Agent(
            agent_id=uuid4(),
            miner_hotkey=f"miner-{uuid4().hex[:8]}",
            name="cohort-agent",
            sha256=uuid4().hex * 2,
            status=AgentStatus.SCORED,
            screening_policy_version=9,
        )
        async with session_maker() as s, s.begin():
            s.add(peer)
            await s.flush()
            s.add(
                Score(
                    agent_id=peer.agent_id,
                    validator_hotkey="validator-0",
                    bench_version=_BENCH_VERSION,
                    run_id=f"run-{peer.agent_id.hex[:8]}",
                    signature=None,
                    seed=7,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=peer_memory,
                    median_ms=100,
                    n=30,
                    details={},
                    generated_at=datetime.now(UTC),
                )
            )
    agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
    _install_db(app, session_maker)
    _install_chain(app)

    await _score_to_quorum(
        client,
        agent_id,
        maker=session_maker,
        composite=0.605,
        tool_mean=0.61,
        memory_mean=0.95,
    )

    async with session_maker() as s:
        agent = await s.get(Agent, agent_id)
        audits = (
            await s.scalars(
                select(ScoreAuditEntry).where(
                    ScoreAuditEntry.agent_id == agent_id,
                    ScoreAuditEntry.event == EVENT_AUDIT,
                )
            )
        ).all()
    assert agent is not None and agent.status == AgentStatus.SCORED
    [audit] = [
        entry
        for entry in audits
        if entry.payload.get("audit_kind")
        in {OUTLIER_REVIEW_KIND, OUTLIER_AXIS_EVIDENCE_KIND}
    ]
    assert audit.payload["audit_kind"] == OUTLIER_AXIS_EVIDENCE_KIND
    evidence = audit.payload["evidence"]
    assert evidence["composite"] == pytest.approx(0.605)
    assert evidence["cohort_size"] == len(_SPREAD_COHORT)
    assert evidence["per_axis"] == _NEUTRAL_AXES


def test_public_audit_evidence_keeps_only_neutral_axis_fields() -> None:
    decision = evaluate_score_outlier(
        composite=0.92,
        cohort=_HIGH_COHORT,
        settings=_enforce(per_axis_enforce=True),
        axes=_axes(0.91, 0.95, tool_cohort=_HIGH_COHORT),
    )
    public = public_audit_evidence(decision.evidence)
    assert public["per_axis"] == _NEUTRAL_AXES
    assert public["trigger"] == "per_axis"
    assert "per_axis_enforce" not in public
    assert "per_axis_outlier_axes" not in public
    # The private decision itself is untouched.
    assert "cohort_median" in _axis(decision.evidence, "memory_mean")


@pytest.mark.parametrize(
    ("composite", "cohort"),
    [(0.99, _SPREAD_COHORT), (0.615, _SPREAD_COHORT), (0.99, [0.5, 0.51])],
)
def test_public_audit_evidence_is_identity_without_axes(
    composite: float, cohort: list[float]
) -> None:
    """Composite-only evidence publishes exactly what it always has."""
    evidence = evaluate_score_outlier(
        composite=composite, cohort=cohort, settings=_enforce()
    ).evidence
    public = public_audit_evidence(evidence)
    assert public == evidence
    assert list(public) == list(evidence)
