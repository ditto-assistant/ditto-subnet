"""Read-only activity of the anomalous-score outlier escalation (issue #476).

Both escalation modes already leave an append-only trail on the hash-chained
score audit log: ``observe`` appends a would-be hold, ``enforce`` opens an
``ath_reviews`` row AND appends the same entry with ``enforced: true``. This
module only reads that trail (plus the pending outlier review count) so an
operator can see what the gate has done. It never writes.

Every list is bounded; the counts are exact aggregates over the whole chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.outlier_escalation import OUTLIER_REVIEW_KIND
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


@dataclass(frozen=True)
class OutlierEscalationEntry:
    """One observe-mode would-be hold or enforced hold from the audit chain."""

    seq: int
    agent_id: UUID
    recorded_at: datetime
    enforced: bool
    bench_version: int | None
    algorithm_version: str | None
    evidence: dict[str, float | int | bool | None]


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


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _public_evidence(raw: Any) -> dict[str, float | int | bool | None]:
    evidence = raw if isinstance(raw, dict) else {}
    safe: dict[str, float | int | bool | None] = {}
    for key in _EVIDENCE_NUMBERS:
        safe[key] = _number(evidence.get(key))
    for key in _EVIDENCE_INTS:
        safe[key] = _integer(evidence.get(key))
    for key in _EVIDENCE_BOOLS:
        value = evidence.get(key)
        safe[key] = value if isinstance(value, bool) else None
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
        evidence=_public_evidence(evidence),
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
    is_outlier = (
        ScoreAuditEntry.event == EVENT_AUDIT,
        ScoreAuditEntry.payload["audit_kind"].as_string() == OUTLIER_REVIEW_KIND,
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

    rows = (
        await session.execute(
            select(
                ScoreAuditEntry.seq,
                ScoreAuditEntry.agent_id,
                ScoreAuditEntry.recorded_at,
                ScoreAuditEntry.payload,
            )
            .where(*is_outlier)
            .order_by(ScoreAuditEntry.seq.desc())
            .limit(limit + 1)
        )
    ).all()

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
    )
