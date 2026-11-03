"""Read-only activity of the anomalous-score outlier escalation (issue #476).

Both escalation modes already leave an append-only trail on the hash-chained
score audit log: ``observe`` appends a would-be hold, ``enforce`` opens an
``ath_reviews`` row AND appends the same entry with ``enforced: true``. This
module only reads that trail (plus the pending outlier review count) so an
operator can see what the gate has done. It never writes.

A per-axis outlier the policy does not hold is appended under its own
``anomalous_score_axis`` kind (evidence only), so it is counted and listed
separately and never inflates the would-be-hold or hold counts. The chain is
public, so per-axis evidence on it is only the axis name and outlier flag;
per-axis statistics are null here and come from the dry-run replay or the
held row's ``ath_reviews`` snapshot.

Every list is bounded; the counts are exact aggregates over the whole chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.outlier_escalation import (
    OUTLIER_AXIS_EVIDENCE_KIND,
    OUTLIER_REVIEW_KIND,
)
from ditto.db.models import AthReview, ScoreAuditEntry
from ditto.db.queries.audit import EVENT_AUDIT

# Evidence scalars the escalation already records on every entry. Only these
# are surfaced, and only when they have the recorded scalar type, so a future
# evidence field (or an unexpected shape) never leaks through by default.
_EVIDENCE_NUMBERS = (
    "composite",
    "cohort_median",
    "cohort_mad",
    "modified_z",
    "modified_z_threshold",
    "min_composite_floor",
)
_EVIDENCE_INTS = ("cohort_size", "min_cohort_size")
_EVIDENCE_BOOLS = ("upward", "above_floor")
_AXIS_NUMBERS = ("value", "cohort_median", "cohort_mad", "modified_z")
_AXIS_BOOLS = ("upward", "outlier")
_TRIGGERS = frozenset({"composite", "per_axis"})
_UNAVAILABLE = frozenset({"cohort_too_small"})
# A bound on the per-axis list one entry may surface; the gate records two.
_MAX_AXES = 8

EvidenceValue = float | int | bool | str | list[Any] | None


@dataclass(frozen=True)
class OutlierEscalationEntry:
    """One observe-mode would-be hold or enforced hold from the audit chain."""

    seq: int
    agent_id: UUID
    recorded_at: datetime
    enforced: bool
    bench_version: int | None
    algorithm_version: str | None
    evidence: dict[str, EvidenceValue]


@dataclass(frozen=True)
class OutlierEscalationActivity:
    window_hours: int
    window_started_at: datetime
    observed_total: int
    enforced_total: int
    observed_in_window: int
    enforced_in_window: int
    latest_recorded_at: datetime | None
    pending_review_count: int
    recent_limit: int
    recent: list[OutlierEscalationEntry]
    recent_truncated: bool
    axis_evidence_total: int = 0
    axis_evidence_in_window: int = 0
    recent_axis_evidence: list[OutlierEscalationEntry] = field(default_factory=list)
    recent_axis_evidence_truncated: bool = False


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _axis_evidence(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("axis"), str):
        return None
    safe: dict[str, Any] = {"axis": raw["axis"]}
    for key in _AXIS_NUMBERS:
        safe[key] = _number(raw.get(key))
    safe["cohort_size"] = _integer(raw.get("cohort_size"))
    for key in _AXIS_BOOLS:
        safe[key] = _bool(raw.get(key))
    unavailable = raw.get("anomaly_unavailable")
    safe["anomaly_unavailable"] = unavailable if unavailable in _UNAVAILABLE else None
    return safe


def public_outlier_evidence(raw: Any) -> dict[str, EvidenceValue]:
    """The typed, allow-listed projection of one recorded evidence snapshot.

    Shared by the activity read and the dry-run replay so both report the
    same fields with the same nulling of absent or mistyped values.
    """
    evidence = raw if isinstance(raw, dict) else {}
    safe: dict[str, EvidenceValue] = {}
    for key in _EVIDENCE_NUMBERS:
        safe[key] = _number(evidence.get(key))
    for key in _EVIDENCE_INTS:
        safe[key] = _integer(evidence.get(key))
    for key in _EVIDENCE_BOOLS:
        safe[key] = _bool(evidence.get(key))
    per_axis = evidence.get("per_axis")
    if isinstance(per_axis, list):
        safe["per_axis"] = [
            axis
            for axis in (_axis_evidence(item) for item in per_axis[:_MAX_AXES])
            if axis is not None
        ]
    else:
        safe["per_axis"] = None
    outlier_axes = evidence.get("per_axis_outlier_axes")
    if isinstance(outlier_axes, list):
        safe["per_axis_outlier_axes"] = [
            axis for axis in outlier_axes[:_MAX_AXES] if isinstance(axis, str)
        ]
    elif isinstance(safe["per_axis"], list):
        # The public audit chain records only axis + flag; derive the list.
        safe["per_axis_outlier_axes"] = [
            entry["axis"] for entry in safe["per_axis"] if entry["outlier"] is True
        ]
    else:
        safe["per_axis_outlier_axes"] = None
    safe["per_axis_enforce"] = _bool(evidence.get("per_axis_enforce"))
    trigger = evidence.get("trigger")
    safe["trigger"] = trigger if trigger in _TRIGGERS else None
    return safe


def _entry(
    seq: int, agent_id: UUID, recorded_at: datetime, payload: Any
) -> OutlierEscalationEntry:
    body = payload if isinstance(payload, dict) else {}
    evidence = body.get("evidence")
    algorithm_version = (
        evidence.get("algorithm_version") if isinstance(evidence, dict) else None
    )
    return OutlierEscalationEntry(
        seq=seq,
        agent_id=agent_id,
        recorded_at=recorded_at,
        enforced=body.get("enforced") is True,
        bench_version=_integer(body.get("bench_version")),
        algorithm_version=(
            algorithm_version if isinstance(algorithm_version, str) else None
        ),
        evidence=public_outlier_evidence(evidence),
    )


async def load_outlier_escalation_activity(
    session: AsyncSession,
    *,
    now: datetime,
    window_hours: int,
    limit: int,
) -> OutlierEscalationActivity:
    """Summarize the escalation's audit-chain trail and pending holds."""
    window_started_at = now - timedelta(hours=window_hours)
    audit_kind = ScoreAuditEntry.payload["audit_kind"].as_string()
    is_outlier = (
        ScoreAuditEntry.event == EVENT_AUDIT,
        audit_kind == OUTLIER_REVIEW_KIND,
    )
    is_axis_evidence = (
        ScoreAuditEntry.event == EVENT_AUDIT,
        audit_kind == OUTLIER_AXIS_EVIDENCE_KIND,
    )
    # Text comparison, not a boolean cast: a cast errors on a JSON null, and an
    # operator read must never fail on one odd historical row.
    enforced_text = ScoreAuditEntry.payload["enforced"].as_string()
    enforced = enforced_text == "true"
    observed = enforced_text == "false"
    in_window = ScoreAuditEntry.recorded_at >= window_started_at
    counts = (
        await session.execute(
            select(
                func.count().filter(observed),
                func.count().filter(enforced),
                func.count().filter(observed, in_window),
                func.count().filter(enforced, in_window),
                func.max(ScoreAuditEntry.recorded_at),
            ).where(*is_outlier)
        )
    ).one()
    axis_counts = (
        await session.execute(
            select(func.count(), func.count().filter(in_window)).where(
                *is_axis_evidence
            )
        )
    ).one()

    async def _recent(*where: Any) -> list[Any]:
        return list(
            (
                await session.execute(
                    select(
                        ScoreAuditEntry.seq,
                        ScoreAuditEntry.agent_id,
                        ScoreAuditEntry.recorded_at,
                        ScoreAuditEntry.payload,
                    )
                    .where(*where)
                    .order_by(ScoreAuditEntry.seq.desc())
                    .limit(limit + 1)
                )
            ).all()
        )

    rows = await _recent(*is_outlier)
    axis_rows = await _recent(*is_axis_evidence)

    pending = await session.scalar(
        select(func.count())
        .select_from(AthReview)
        .where(
            AthReview.status == "pending",
            AthReview.algorithm_provenance["review_kind"].as_string()
            == OUTLIER_REVIEW_KIND,
        )
    )

    return OutlierEscalationActivity(
        window_hours=window_hours,
        window_started_at=window_started_at,
        observed_total=int(counts[0]),
        enforced_total=int(counts[1]),
        observed_in_window=int(counts[2]),
        enforced_in_window=int(counts[3]),
        latest_recorded_at=counts[4],
        pending_review_count=int(pending or 0),
        recent_limit=limit,
        recent=[
            _entry(row.seq, row.agent_id, row.recorded_at, row.payload)
            for row in rows[:limit]
        ],
        recent_truncated=len(rows) > limit,
        axis_evidence_total=int(axis_counts[0]),
        axis_evidence_in_window=int(axis_counts[1]),
        recent_axis_evidence=[
            _entry(row.seq, row.agent_id, row.recorded_at, row.payload)
            for row in axis_rows[:limit]
        ],
        recent_axis_evidence_truncated=len(axis_rows) > limit,
    )
