"""Configurable, observability-only thresholds for the ordinary source-review
queue-age SLO (ditto-subnet#2042, vertical slice 1).

Both thresholds default unset (``None``) on purpose. Peyton was explicit:
"Do not invent p95 or maximum-age policy numbers... an unset threshold
[reports] as null, not zero or 'healthy'." The v13 screening policy's
recommended 24 hours is not an established live finalizer deadline, so it is
not shipped here as a default. An operator who wants overdue counting sets
one or both env vars; until then the endpoints report ``null`` for every
threshold-derived field.

This module intentionally mirrors the small env-parsed config pattern used by
sibling optional features (see ``ditto.api_server.validator_names``) rather
than a new ``*SettingsRevision`` table: these thresholds are read-only and
enforce nothing yet (no alert, no operator escalation action), so a
redeploy-to-change env var is a smaller surface than a new audited,
append-only settings table. Revisit that choice -- likely moving to a
``SourceReviewQueueSloSettingsRevision`` table matching
``ditto.api_server.queue_policy_settings`` -- when a follow-up PR adds live
enforcement/alerting and an operator needs to tune it without a deploy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceReviewQueueSloConfig:
    """Observability-only overdue thresholds for the ordinary review queue."""

    max_actionable_age_threshold_seconds: int | None = None
    """Age past which an actionable ordinary-review item counts as overdue.
    ``None`` (the shipped default) means overdue counting is off: the
    snapshot reports ``overdue_count = null``, never ``0``."""

    p95_age_threshold_seconds: int | None = None
    """Reference p95 age used only to report whether the observed p95
    exceeds it (``p95_exceeds_threshold``). ``None`` reports that field as
    ``null``. Never enforced -- no alert fires off this value in this PR."""


def parse_source_review_queue_slo_config_from_env() -> SourceReviewQueueSloConfig:
    """Resolve the optional overdue thresholds from env, unset by default."""

    def _optional_int(name: str) -> int | None:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return None
        return int(raw)

    return SourceReviewQueueSloConfig(
        max_actionable_age_threshold_seconds=_optional_int(
            "DITTO_SOURCE_REVIEW_QUEUE_MAX_AGE_THRESHOLD_SECONDS"
        ),
        p95_age_threshold_seconds=_optional_int(
            "DITTO_SOURCE_REVIEW_QUEUE_P95_AGE_THRESHOLD_SECONDS"
        ),
    )
