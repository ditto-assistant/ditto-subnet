"""Observability thresholds for the ATH-hold queue-age SLO.

Vertical slice 2 of ditto-subnet#2042 shipped every threshold unset, so the
live read reported ``overdue_count`` and ``p95_exceeds_threshold`` as null for
every class. On 2026-11-22 that hid a copy hold pending for ten days and a
benchmark-overfit escalation pending for about 128 days (SN-499).

The env parser now applies the shipped per-class defaults below. They are
review-latency targets that make overdue holds visible. They are not ruling
policy: nothing here clears, rejects, alerts, or escalates a hold. Each value
can be overridden with its env var, and ``off`` disables one threshold, which
then reports null again.

The dataclass defaults stay unset, so a hand-built config (tests, scripts)
still means "no thresholds" unless it opts in.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ditto.api_server.errors import ApiServerConfigError
from ditto.db.queries.ath_review_queue_slo import (
    ATH_QUEUE_CLASSES,
    AthQueueClass,
    QueueAgeThresholds,
)

_HOUR = 3600

ATH_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV = (
    "DITTO_ATH_REVIEW_QUEUE_MAX_AGE_THRESHOLD_SECONDS"
)
ATH_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV = (
    "DITTO_ATH_REVIEW_QUEUE_P95_AGE_THRESHOLD_SECONDS"
)
# The copy class keeps the env names slice 2 shipped.
COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV = (
    "DITTO_COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_SECONDS"
)
COPY_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV = (
    "DITTO_COPY_REVIEW_QUEUE_P95_AGE_THRESHOLD_SECONDS"
)

DISABLED_THRESHOLD_VALUES = frozenset({"off", "none", "unset"})
"""Env values that turn one shipped threshold off, so it reports null."""

DEFAULT_ATH_THRESHOLDS = QueueAgeThresholds(
    max_actionable_age_seconds=72 * _HOUR, p95_age_seconds=48 * _HOUR
)
"""The aggregate over every pending hold: no hold of any kind waits past
three days."""

DEFAULT_CLASS_THRESHOLDS: Mapping[AthQueueClass, QueueAgeThresholds] = MappingProxyType(
    {
        # Copy holds block a scored agent from the board on a
        # mechanical signal. The copy court has already written its
        # evidence, so a human ruling should take hours, not days.
        "copy": QueueAgeThresholds(
            max_actionable_age_seconds=48 * _HOUR,
            p95_age_seconds=24 * _HOUR,
        ),
        # Overfit audits need source reading against the bench.
        "benchmark_overfit": QueueAgeThresholds(
            max_actionable_age_seconds=72 * _HOUR,
            p95_age_seconds=48 * _HOUR,
        ),
        # The deep pass is automated; this clock includes the screener
        # wait and the operator ruling that follows it.
        "deferred_source_review": QueueAgeThresholds(
            max_actionable_age_seconds=48 * _HOUR,
            p95_age_seconds=24 * _HOUR,
        ),
        # Top-five rows hold paid slots, so they get the tightest clock.
        "integrity_double_check": QueueAgeThresholds(
            max_actionable_age_seconds=24 * _HOUR,
            p95_age_seconds=12 * _HOUR,
        ),
        "anomalous_score": QueueAgeThresholds(
            max_actionable_age_seconds=48 * _HOUR,
            p95_age_seconds=24 * _HOUR,
        ),
    }
)
"""Shipped per-class review-latency targets (SN-499). Override per env var."""


def class_threshold_env_names(queue_class: AthQueueClass) -> tuple[str, str]:
    """The (max age, p95) env var names for one class."""
    if queue_class == "copy":
        return (
            COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV,
            COPY_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV,
        )
    stem = f"DITTO_ATH_REVIEW_QUEUE_{queue_class.upper()}"
    return (
        f"{stem}_MAX_AGE_THRESHOLD_SECONDS",
        f"{stem}_P95_AGE_THRESHOLD_SECONDS",
    )


@dataclass(frozen=True)
class AthReviewQueueSloConfig:
    """Observability-only overdue thresholds for the ATH-hold queue."""

    ath: QueueAgeThresholds = field(default_factory=QueueAgeThresholds)
    """Thresholds for the aggregate over every pending ATH hold."""

    classes: Mapping[AthQueueClass, QueueAgeThresholds] = field(
        default_factory=lambda: MappingProxyType({})
    )
    """Thresholds per queue class. A missing class reports null."""

    @property
    def copy(self) -> QueueAgeThresholds:
        """Thresholds for the copy-review class."""
        return self.classes.get("copy", QueueAgeThresholds())


def _threshold(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    if raw.strip().lower() in DISABLED_THRESHOLD_VALUES:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiServerConfigError(
            f"{name} must be a positive integer or 'off', got {raw!r}"
        ) from exc
    if value <= 0:
        # The wire model requires gt=0. Fail at boot, not on every read.
        raise ApiServerConfigError(f"{name} must be positive, got {value}")
    return value


def _thresholds(
    max_age_env: str, p95_env: str, default: QueueAgeThresholds
) -> QueueAgeThresholds:
    return QueueAgeThresholds(
        max_actionable_age_seconds=_threshold(
            max_age_env, default.max_actionable_age_seconds
        ),
        p95_age_seconds=_threshold(p95_env, default.p95_age_seconds),
    )


def parse_ath_review_queue_slo_config_from_env() -> AthReviewQueueSloConfig:
    """Resolve the thresholds: shipped defaults, each overridable from env."""
    return AthReviewQueueSloConfig(
        ath=_thresholds(
            ATH_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV,
            ATH_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV,
            DEFAULT_ATH_THRESHOLDS,
        ),
        classes=MappingProxyType(
            {
                queue_class: _thresholds(
                    *class_threshold_env_names(queue_class),
                    DEFAULT_CLASS_THRESHOLDS[queue_class],
                )
                for queue_class in ATH_QUEUE_CLASSES
            }
        ),
    )
