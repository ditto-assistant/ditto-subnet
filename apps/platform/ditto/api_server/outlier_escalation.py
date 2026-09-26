"""Auto-escalate out-of-band benchmark composites to ATH review (issue #476).

When a fresh benchmark contract is gamed, a submission can spike to a
near-perfect / statistical-outlier composite before the ordinary scoring gates
(anti-copy, transform audit, deferred source review) catch it. Left alone that
row auto-ranks and takes emission weight the moment its quorum finalizes. This
module supplies the *safety net*: a robust cohort-outlier test that, on the
score-finalization transition, routes such a row to ``ath_pending_review`` for
operator adjudication INSTEAD OF ranking it.

This is a review **hold, never a rejection**. A high composite is not evidence
of misconduct by itself -- it might be a genuinely excellent agent, a
benchmark-specific compiler, or a scoring/infrastructure anomaly, and only an
operator can tell those apart. So the outcome is exactly the one the copy and
transform-audit holds already use: the agent is parked in ``ATH_PENDING_REVIEW``
with an immutable evidence snapshot, excluded from the emission-eligible ledger,
and released or rejected only through the normal audited review flow.

The statistic is deliberately robust. A cohort of honest composites plus one
gamed spike would blow up a mean/stdev z-score (the spike inflates the very
scale it is measured against), so the cohort centre is the **median** and the
scale is the **median absolute deviation (MAD)** -- the same estimators the
deferred-source-review anomaly path uses. The candidate holds only when it is
BOTH far in MAD terms AND above an absolute high-composite floor, so the gate
fires only on *upward* spikes near the top of the scale, never on an ordinary
row that happens to sit a few MADs off a tight cohort.

The function is pure and deterministic: the same cohort and composite always
yield the same verdict, so re-scoring an agent can never flip its fate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median
from typing import Literal

# ---------------------------------------------------------------------------
# Defaults. These ship as the shipped behaviour and are overridable from the
# environment (see ``ditto.api_server.endpoints.validator``), mirroring the
# transform-audit constants (``AUDIT_ALPHA`` / ``TRANSFORM_AUDIT_ENFORCE``).
# ---------------------------------------------------------------------------

# The benchmark generation this safety net is scoped to. v8-v11 keep their exact
# prior behaviour: the gate is a no-op below this version. It exists for v12+,
# where a freshly refreshed contract has the least cohort history and is the
# most exposed to a first-mover gaming spike.
DEFAULT_MIN_BENCH_VERSION = 12

# Fewest comparable peers required before the statistic means anything. Below
# this the cohort is too thin for a median/MAD to be a stable baseline, so the
# gate FAILS CLOSED -- it records "insufficient data" and never holds, rather
# than inventing an anomaly decision from three points.
DEFAULT_MIN_COHORT_SIZE = 8

# Modified z-score (MAD distance) beyond which the composite is out-of-band.
# 3.5 is the classic Iglewicz-Hoaglin outlier cutoff; 6.0 is intentionally far
# more conservative because a false hold is expensive for an honest miner and a
# hold only *delays* a genuinely great agent until an operator clears it.
DEFAULT_MODIFIED_Z_THRESHOLD = 6.0

# Absolute composite floor. Only a spike at or above this ranks-threatening
# level can hold, so the gate never quarantines a mediocre-but-noisy row that is
# statistically odd yet nowhere near taking weight. This is the "only UPWARD
# spikes near the top hold" guardrail from issue #476.
DEFAULT_MIN_COMPOSITE_FLOOR = 0.90

# The consistency constant that scales MAD to an estimate of the standard
# deviation for normally distributed data (0.6745 = Phi^-1(0.75)). Multiplying
# by it makes the modified z-score comparable to an ordinary z-score, so the
# threshold reads in familiar sigma units.
_MAD_TO_SIGMA = 0.6744897501960817

# The neutral, non-accusatory reason string written to ``agents.review_reason``
# and ``ath_reviews.original_reason``. "Pending review", not "cheating detected"
# -- a high score is not misconduct. Both of those columns render on the PUBLIC
# board, so this deliberately carries NO cohort statistics or thresholds: the
# numbers live only in ``ath_reviews.original_evidence`` (operator/Backroom
# only), keeping sensitive thresholds out of public UI and logs (issue #540).
OUTLIER_REVIEW_REASON = "Anomalous benchmark score pending operator review"

# ``algorithm_provenance.review_kind`` for the holds this module opens, so the
# operator queue can filter them apart from copy / overfit / deferred holds.
OUTLIER_REVIEW_KIND = "anomalous_score"

# Bumped whenever the statistic or its wiring changes, so an operator can tell
# which rule version produced a given held row.
OUTLIER_ALGORITHM_VERSION = "outlier-escalation-v1"


@dataclass(frozen=True)
class OutlierEscalationSettings:
    """Tunables for the out-of-band composite escalation.

    ``mode`` mirrors the deferred-source-review board's rollout ladder:

    ``off``      the gate is never computed (byte-identical to pre-#476).
    ``observe``  the gate is computed and a would-be hold is recorded to the
                 append-only audit chain, but the agent is NOT held. This is the
                 rollout-and-measure mode issue #476 asks to start in.
    ``enforce``  a would-be hold is applied: the agent moves to
                 ``ATH_PENDING_REVIEW`` and is excluded from ranking.
    """

    mode: str = "off"
    min_bench_version: int = DEFAULT_MIN_BENCH_VERSION
    min_cohort_size: int = DEFAULT_MIN_COHORT_SIZE
    modified_z_threshold: float = DEFAULT_MODIFIED_Z_THRESHOLD
    min_composite_floor: float = DEFAULT_MIN_COMPOSITE_FLOOR


OUTLIER_ESCALATION_MODES: frozenset[str] = frozenset({"off", "observe", "enforce"})

# The environment variable behind each tunable. Operators read these names back
# through the admin posture endpoint; the values themselves are never echoed.
OUTLIER_ESCALATION_ENV_VARS: Mapping[str, str] = {
    "mode": "DITTO_OUTLIER_ESCALATION_MODE",
    "min_bench_version": "DITTO_OUTLIER_ESCALATION_MIN_BENCH_VERSION",
    "min_cohort_size": "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE",
    "modified_z_threshold": "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD",
    "min_composite_floor": "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR",
}

# Where one effective value came from. ``default_invalid_env`` means the
# variable WAS set but was rejected, so the shipped default is in force -- the
# silent fallback an operator otherwise cannot see.
OutlierSettingSource = Literal["env", "default", "default_invalid_env"]


@dataclass(frozen=True)
class OutlierEscalationSettingSources:
    """Per-field provenance of the loaded :class:`OutlierEscalationSettings`."""

    mode: OutlierSettingSource = "default"
    min_bench_version: OutlierSettingSource = "default"
    min_cohort_size: OutlierSettingSource = "default"
    modified_z_threshold: OutlierSettingSource = "default"
    min_composite_floor: OutlierSettingSource = "default"


@dataclass(frozen=True)
class OutlierEscalationSettingsLoad:
    """The settings scoring uses, plus how each field was resolved.

    ``settings`` is exactly what the pre-existing env builder returned; the
    sources are a read-only side record and never feed back into scoring.
    """

    settings: OutlierEscalationSettings
    sources: OutlierEscalationSettingSources
    loaded_at: datetime


def load_outlier_escalation_settings(
    environ: Mapping[str, str], *, now: datetime | None = None
) -> OutlierEscalationSettingsLoad:
    """Resolve the escalation policy from ``environ`` and record each source.

    Fallback behaviour is unchanged from the original env builder: an unset
    variable, an unknown mode, or an unparseable number degrades to the shipped
    default rather than crashing scoring. Parsing is the same ``strip().lower()``
    for the mode and bare ``int()`` / ``float()`` for the numbers, so any value
    accepted before is accepted now and resolves to the same number. The raw
    rejected string is deliberately not retained.
    """
    defaults = OutlierEscalationSettings()
    env = OUTLIER_ESCALATION_ENV_VARS

    raw_mode = environ.get(env["mode"])
    mode_source: OutlierSettingSource
    if raw_mode is None:
        mode, mode_source = defaults.mode, "default"
    else:
        candidate = raw_mode.strip().lower()
        if candidate in OUTLIER_ESCALATION_MODES:
            mode, mode_source = candidate, "env"
        else:
            mode, mode_source = defaults.mode, "default_invalid_env"

    def _int(field_name: str, fallback: int) -> tuple[int, OutlierSettingSource]:
        raw = environ.get(env[field_name])
        if raw is None:
            return fallback, "default"
        try:
            return int(raw), "env"
        except ValueError:
            return fallback, "default_invalid_env"

    def _float(field_name: str, fallback: float) -> tuple[float, OutlierSettingSource]:
        raw = environ.get(env[field_name])
        if raw is None:
            return fallback, "default"
        try:
            return float(raw), "env"
        except ValueError:
            return fallback, "default_invalid_env"

    min_bench_version, min_bench_version_source = _int(
        "min_bench_version", defaults.min_bench_version
    )
    min_cohort_size, min_cohort_size_source = _int(
        "min_cohort_size", defaults.min_cohort_size
    )
    modified_z_threshold, modified_z_threshold_source = _float(
        "modified_z_threshold", defaults.modified_z_threshold
    )
    min_composite_floor, min_composite_floor_source = _float(
        "min_composite_floor", defaults.min_composite_floor
    )
    return OutlierEscalationSettingsLoad(
        settings=OutlierEscalationSettings(
            mode=mode,
            min_bench_version=min_bench_version,
            min_cohort_size=min_cohort_size,
            modified_z_threshold=modified_z_threshold,
            min_composite_floor=min_composite_floor,
        ),
        sources=OutlierEscalationSettingSources(
            mode=mode_source,
            min_bench_version=min_bench_version_source,
            min_cohort_size=min_cohort_size_source,
            modified_z_threshold=modified_z_threshold_source,
            min_composite_floor=min_composite_floor_source,
        ),
        loaded_at=now if now is not None else datetime.now(UTC),
    )


@dataclass(frozen=True)
class OutlierDecision:
    """Outcome of the outlier test for one finalized composite.

    ``held`` means the row is out-of-band and (in ``enforce`` mode) should be
    routed to ATH review instead of ranked. ``evidence`` is the immutable
    snapshot -- cohort stats and the candidate composite -- persisted on the
    review so the operator sees exactly WHY it held. It is always populated,
    even when ``held`` is False, so ``observe`` mode and the "insufficient data"
    branch leave an auditable record.
    """

    held: bool
    reason: str | None = None
    evidence: dict[str, object] = field(default_factory=dict)


def _median_mad(values: Sequence[float]) -> tuple[float, float]:
    """Median centre and median absolute deviation of ``values``.

    Both are robust to a single extreme member, which is the whole point: a
    gamed spike must not be able to move the baseline it is measured against.
    """
    center = float(median(values))
    return center, float(median(abs(value - center) for value in values))


def evaluate_score_outlier(
    *,
    composite: float,
    cohort: Sequence[float],
    settings: OutlierEscalationSettings,
) -> OutlierDecision:
    """Decide whether ``composite`` is an out-of-band upward spike vs ``cohort``.

    ``cohort`` is the comparable same-benchmark peer set (the eligible ledger's
    composites), which already excludes the candidate itself, held agents, and
    banned agents. The candidate holds iff ALL of:

    * the cohort has at least ``min_cohort_size`` members (else fail closed,
      recording ``anomaly_unavailable = "cohort_too_small"``);
    * the composite is a strict *upward* deviation (``composite > median``);
    * its modified z-score (MAD distance) is at or beyond
      ``modified_z_threshold``; and
    * it sits at or above the absolute ``min_composite_floor``.

    Ties / a degenerate cohort (``mad == 0``) are handled without dividing by
    zero: an identical cohort has no spread, so any strictly-upward composite
    above the floor is treated as out-of-band and the modified z-score is
    recorded as ``None`` rather than an infinity that JSON cannot store.
    """
    peers = [float(value) for value in cohort]
    cohort_size = len(peers)
    composite = float(composite)

    evidence: dict[str, object] = {
        "composite": composite,
        "cohort_size": cohort_size,
        "min_cohort_size": settings.min_cohort_size,
        "modified_z_threshold": settings.modified_z_threshold,
        "min_composite_floor": settings.min_composite_floor,
        "algorithm_version": OUTLIER_ALGORITHM_VERSION,
    }

    # Fail closed on a thin cohort: too few points for a median/MAD to be a
    # meaningful baseline. Record the condition; never invent a decision.
    if cohort_size < settings.min_cohort_size:
        evidence["anomaly_unavailable"] = "cohort_too_small"
        return OutlierDecision(held=False, reason=None, evidence=evidence)

    center, mad = _median_mad(peers)
    upward = composite > center
    above_floor = composite >= settings.min_composite_floor

    if mad == 0.0:
        # Degenerate cohort: no spread to divide by. Any strictly-upward
        # composite is "infinitely" far, but we record None rather than inf.
        modified_z: float | None = None
        beyond_distance = upward
    else:
        modified_z = _MAD_TO_SIGMA * (composite - center) / mad
        beyond_distance = modified_z >= settings.modified_z_threshold

    evidence.update(
        {
            "cohort_median": center,
            "cohort_mad": mad,
            "modified_z": modified_z,
            "upward": upward,
            "above_floor": above_floor,
        }
    )

    held = bool(beyond_distance and upward and above_floor)
    if not held:
        return OutlierDecision(held=False, reason=None, evidence=evidence)

    # The reason is a fixed NEUTRAL string (no cohort numbers) because it renders
    # publicly; the statistics that justify the hold are in ``evidence``, which
    # only operators see. See ``OUTLIER_REVIEW_REASON``.
    return OutlierDecision(held=True, reason=OUTLIER_REVIEW_REASON, evidence=evidence)
