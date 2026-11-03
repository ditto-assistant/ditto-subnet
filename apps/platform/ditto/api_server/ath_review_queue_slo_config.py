"""Observability-only thresholds for the ATH-hold queue-age SLO.

This is vertical slice 2 of ditto-subnet#2042. It follows
:mod:`ditto.api_server.source_review_queue_slo_config`: every threshold
defaults unset (``None``). #2042 names the clocks but sets no p95 or
maximum-age numbers, and slice 1's review was explicit not to invent them. An
unset threshold reports as ``null``, never as ``0`` or "healthy". An operator
who wants overdue counting sets the env vars named below.

There are two clocks. The ATH aggregate covers every pending hold. The copy
class covers ``review_kind = copy`` and legacy holds with no kind. The other
classes report null thresholds until a follow-up gives them their own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ditto.api_server.errors import ApiServerConfigError
from ditto.db.queries.ath_review_queue_slo import QueueAgeThresholds

ATH_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV = (
    "DITTO_ATH_REVIEW_QUEUE_MAX_AGE_THRESHOLD_SECONDS"
)
ATH_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV = (
    "DITTO_ATH_REVIEW_QUEUE_P95_AGE_THRESHOLD_SECONDS"
)
COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV = (
    "DITTO_COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_SECONDS"
)
COPY_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV = (
    "DITTO_COPY_REVIEW_QUEUE_P95_AGE_THRESHOLD_SECONDS"
)


@dataclass(frozen=True)
class AthReviewQueueSloConfig:
    """Observability-only overdue thresholds for the ATH-hold queue."""

    ath: QueueAgeThresholds = field(default_factory=QueueAgeThresholds)
    """Thresholds for the aggregate over every pending ATH hold."""

    copy: QueueAgeThresholds = field(default_factory=QueueAgeThresholds)
    """Thresholds for the copy-review class."""


def _optional_positive_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiServerConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        # The wire model requires gt=0. Fail at boot, not on every read.
        raise ApiServerConfigError(f"{name} must be positive, got {value}")
    return value


def parse_ath_review_queue_slo_config_from_env() -> AthReviewQueueSloConfig:
    """Resolve the optional thresholds from env. All are unset by default."""
    return AthReviewQueueSloConfig(
        ath=QueueAgeThresholds(
            max_actionable_age_seconds=_optional_positive_int(
                ATH_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV
            ),
            p95_age_seconds=_optional_positive_int(
                ATH_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV
            ),
        ),
        copy=QueueAgeThresholds(
            max_actionable_age_seconds=_optional_positive_int(
                COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV
            ),
            p95_age_seconds=_optional_positive_int(
                COPY_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV
            ),
        ),
    )
