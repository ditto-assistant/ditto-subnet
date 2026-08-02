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
TOP_FIVE_SIZE = 5


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
