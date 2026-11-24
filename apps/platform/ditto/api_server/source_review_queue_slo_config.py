"""Observability thresholds for the ordinary source-review queue-age SLO
(ditto-subnet#2042, vertical slice 1).

Slice 1 shipped both thresholds unset, so the live read reported every
threshold-derived field as null. On 2026-11-22 that hid five items parked in
``infrastructure_backoff``, the oldest about 69 days old (SN-499).

The env parser now applies the shipped defaults below. The max age follows the
v13 screening policy's recommended 24 hours. These are review-latency targets
that make overdue items visible, not a finalizer deadline: nothing here
retries, releases, rejects, alerts, or escalates an item. Override either value
with its env var, or set it to ``off`` to report null again.

The dataclass defaults stay unset, so a hand-built config (tests, scripts)
still means "no thresholds" unless it opts in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ditto.api_server.errors import ApiServerConfigError

SOURCE_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV = (
    "DITTO_SOURCE_REVIEW_QUEUE_MAX_AGE_THRESHOLD_SECONDS"
)
SOURCE_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV = (
    "DITTO_SOURCE_REVIEW_QUEUE_P95_AGE_THRESHOLD_SECONDS"
)
DEFAULT_SOURCE_REVIEW_MAX_AGE_SECONDS = 24 * 3600
DEFAULT_SOURCE_REVIEW_P95_AGE_SECONDS = 12 * 3600
_DISABLED_VALUES = frozenset({"off", "none", "unset"})


@dataclass(frozen=True)
class SourceReviewQueueSloConfig:
    """Observability-only overdue thresholds for the ordinary review queue."""

    max_actionable_age_threshold_seconds: int | None = None
    """Age past which an actionable ordinary-review item counts as overdue.
    ``None`` means overdue counting is off: the snapshot reports
    ``overdue_count = null``, never ``0``."""

    p95_age_threshold_seconds: int | None = None
    """Reference p95 age used only to report whether the observed p95
    exceeds it (``p95_exceeds_threshold``). ``None`` reports that field as
    ``null``. Never enforced."""


def _threshold(name: str, default: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    if raw.strip().lower() in _DISABLED_VALUES:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiServerConfigError(
            f"{name} must be a positive integer or 'off', got {raw!r}"
        ) from exc
    if value <= 0:
        raise ApiServerConfigError(f"{name} must be positive, got {value}")
    return value


def parse_source_review_queue_slo_config_from_env() -> SourceReviewQueueSloConfig:
    """Resolve the thresholds: shipped defaults, each overridable from env."""
    return SourceReviewQueueSloConfig(
        max_actionable_age_threshold_seconds=_threshold(
            SOURCE_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV,
            DEFAULT_SOURCE_REVIEW_MAX_AGE_SECONDS,
        ),
        p95_age_threshold_seconds=_threshold(
            SOURCE_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV,
            DEFAULT_SOURCE_REVIEW_P95_AGE_SECONDS,
        ),
    )
