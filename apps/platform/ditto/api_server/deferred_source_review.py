"""Mechanical-first admission and post-score deep source-review decisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from ditto.api_models.queue_policy_settings import DeferredSourceReviewSettings

if TYPE_CHECKING:
    from ditto.db.queries.scores import LedgerRow

DEFERRED_REVIEW_KIND = "deferred_source_review"
DEFERRED_REVIEW_REASON = "Score qualified this submission for deferred source review"
DEFERRED_MECHANICAL_REASON = "deferred-mechanical-admission"
INCONCLUSIVE_REASON_CODE = "source-review-inconclusive"
# Public text written with INCONCLUSIVE_REASON_CODE. The dashboard keys its
# "no finding" treatment on this exact string (SOURCE_REVIEW_INCONCLUSIVE_REASON
# in dashboard/src/components/pipeline/status.ts); a parity test pins the two.
SOURCE_REVIEW_INCONCLUSIVE_PUBLIC_REASON = (
    "Bounded source review was inconclusive; held for review"
)
TOP_FIVE_SIZE = 5
# A second, stronger deep review for every top-five entrant, including ones
# that already passed the full pre-score screen. It shares the deferred hold
# lifecycle (``review_kind`` stays ``deferred_source_review``) so the screener
# re-claim, auto-clear on a clean pass, and operator adjudication are one path.
INTEGRITY_DOUBLE_CHECK_AUDIT_KIND = "integrity_double_check"
INTEGRITY_DOUBLE_CHECK_REASON = (
    "Top-five rank qualified this submission for an integrity double-check"
)
INTEGRITY_DOUBLE_CHECK_ALGORITHM = "integrity-double-check-v1"
INTEGRITY_DOUBLE_CHECK_ACTOR = "platform:integrity-double-check"
# ``algorithm_provenance["trigger"]`` on a double-check hold. The review kind
# stays ``deferred_source_review``; this marker selects the reviewer posture.
INTEGRITY_DOUBLE_CHECK_TRIGGER = "integrity_double_check"


@dataclass(frozen=True)
class DeferredReviewDecision:
    triggered: bool
    triggers: tuple[
        Literal["top_five", "composite_anomaly", "tool_anomaly", "memory_anomaly"], ...
    ]
    rank: int | None
    evidence: dict[str, object]


def _median_mad(values: Sequence[float]) -> tuple[float, float]:
    center = float(median(values))
    return center, float(median(abs(value - center) for value in values))


def evaluate_deferred_review(
    *,
    agent_id: UUID,
    ledger: Sequence[LedgerRow],
    settings: DeferredSourceReviewSettings,
) -> DeferredReviewDecision:
    """Classify one finalized row against the same-version canonical ledger.

    The ledger already applies the platform's one-emission-owner and quorum
    rules. Ranking it again here would create a second definition of top five,
    so this function consumes its canonical order directly.
    """
    candidate = next((row for row in ledger if row.agent_id == agent_id), None)
    if candidate is None or not candidate.eligible:
        return DeferredReviewDecision(False, (), None, {"eligible": False})

    rank = next(
        index for index, row in enumerate(ledger, start=1) if row.agent_id == agent_id
    )
    peers = [row for row in ledger if row.agent_id != agent_id and row.eligible]
    triggers: list[
        Literal["top_five", "composite_anomaly", "tool_anomaly", "memory_anomaly"]
    ] = []
    if rank <= TOP_FIVE_SIZE:
        triggers.append("top_five")

    metrics: dict[str, object] = {
        "eligible": True,
        "rank": rank,
        "cohort_size": len(ledger),
        "peer_count": len(peers),
        "candidate": {
            "composite": candidate.composite,
            "tool_mean": candidate.tool_mean,
            "memory_mean": candidate.memory_mean,
        },
    }
    if len(peers) >= settings.min_cohort_size:
        thresholds: dict[str, dict[str, float]] = {}
        for field, trigger, multiplier, floor in (
            (
                "composite",
                "composite_anomaly",
                settings.composite_mad_multiplier,
                settings.min_composite_delta,
            ),
            (
                "tool_mean",
                "tool_anomaly",
                settings.axis_mad_multiplier,
                settings.min_axis_delta,
            ),
            (
                "memory_mean",
                "memory_anomaly",
                settings.axis_mad_multiplier,
                settings.min_axis_delta,
            ),
        ):
            values = [float(getattr(row, field)) for row in peers]
            center, mad = _median_mad(values)
            threshold = min(1.0, center + max(floor, multiplier * mad))
            thresholds[field] = {
                "median": center,
                "mad": mad,
                "threshold": threshold,
            }
            if float(getattr(candidate, field)) > threshold:
                triggers.append(trigger)  # type: ignore[arg-type]
        metrics["thresholds"] = thresholds
    else:
        metrics["thresholds"] = None
        metrics["anomaly_unavailable"] = "cohort_too_small"

    metrics["triggers"] = list(triggers)
    metrics["mode"] = settings.mode
    return DeferredReviewDecision(bool(triggers), tuple(triggers), rank, metrics)


def evaluate_integrity_double_check(
    *,
    agent_id: UUID,
    ledger: Sequence[LedgerRow],
    held_composites: Sequence[float],
) -> DeferredReviewDecision:
    """Qualify one ledger row for the top-five integrity double-check.

    ``held_composites`` are the composites of submissions already held for a
    post-score deep review at this benchmark version. The canonical ledger
    drops held agents, so without them every hold would promote the next row
    into the top five and the holds would cascade down the whole board while
    reviews are running. A held row at or above the candidate still occupies
    its slot; ties count against the candidate so the cap stays conservative.
    """
    candidate = next((row for row in ledger if row.agent_id == agent_id), None)
    if candidate is None or not candidate.eligible:
        return DeferredReviewDecision(False, (), None, {"eligible": False})
    ledger_rank = next(
        index for index, row in enumerate(ledger, start=1) if row.agent_id == agent_id
    )
    held_above = sum(
        1 for composite in held_composites if composite >= candidate.composite
    )
    rank = ledger_rank + held_above
    triggered = rank <= TOP_FIVE_SIZE
    evidence: dict[str, object] = {
        "eligible": True,
        "rank": rank,
        "ledger_rank": ledger_rank,
        "held_above": held_above,
        "cohort_size": len(ledger),
        "candidate": {
            "composite": candidate.composite,
            "tool_mean": candidate.tool_mean,
            "memory_mean": candidate.memory_mean,
        },
        "triggers": ["top_five"] if triggered else [],
        "integrity_double_check": True,
    }
    return DeferredReviewDecision(
        triggered, ("top_five",) if triggered else (), rank, evidence
    )
