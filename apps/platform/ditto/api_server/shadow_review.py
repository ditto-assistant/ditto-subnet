"""Wire projection for non-authoritative L2/L3 shadow review telemetry.

The screener runs its agentic source reviewer (analyst -> critic -> adjudicators)
in ``shadow`` mode alongside the authoritative L1 screen. Its verdict is
advisory: by design it can neither quarantine, reject, nor ban. It reaches
operators through two reads that must not drift apart --- the settings board's
recent-observation feed and the per-quarantine review context --- so the row
projection lives here rather than being spelled out at each call site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ditto.api_models.screener_review_settings import AdminShadowReviewObservation

if TYPE_CHECKING:
    from ditto.db.models import ScreenerShadowReview

__all__ = ["shadow_review_observation"]


def shadow_review_observation(
    row: ScreenerShadowReview,
) -> AdminShadowReviewObservation:
    """Project one persisted shadow review onto its admin wire model.

    The JSON columns are copied into fresh containers: SQLAlchemy hands back
    the mutable instance it tracks, and Pydantic would otherwise keep a
    reference to the session's object on a ``frozen=True`` model.
    """
    return AdminShadowReviewObservation.model_validate(
        {
            "attempt_id": row.attempt_id,
            "agent_id": row.agent_id,
            "settings_revision": row.settings_revision,
            "settings_scope": row.settings_scope,
            "settings_checksum": row.settings_checksum,
            "disposition": row.disposition,
            "risk_level": row.risk_level,
            "categories": list(row.categories),
            "finding_digest": row.finding_digest,
            "resolution_basis": row.resolution_basis,
            "clearance_path": row.clearance_path,
            "critic_disposition": row.critic_disposition,
            "adjudicator_disposition": row.adjudicator_disposition,
            "response_models": list(row.response_models),
            "response_providers": list(row.response_providers),
            "usage": dict(row.usage),
            "created_at": row.created_at,
        }
    )
