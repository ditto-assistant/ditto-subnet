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

The composite is a 0.6/0.4 blend of ``tool_mean`` and ``memory_mean``, so one
gamed axis can sit far out of band while the blend stays in band. Each axis is
therefore given the same median/MAD robust z against the same cohort, under the
same fail-closed cohort rule, and recorded as per-axis evidence. A per-axis
outlier is observe-only evidence by default: it holds only when the gate is in
``enforce`` mode AND ``per_axis_enforce`` is on, and the composite verdict is
unchanged either way.

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

# ``score_audit_log`` ``audit_kind`` for a per-axis outlier that is recorded as
# evidence only: an axis far out of band while the policy would NOT hold the
# row (the composite is in band and per-axis enforcement is off). Kept apart
# from ``OUTLIER_REVIEW_KIND`` so the would-be-hold / hold counts the posture
# read reports keep meaning exactly "rows this policy holds or would hold".
OUTLIER_AXIS_EVIDENCE_KIND = "anomalous_score_axis"

# Bumped whenever the statistic or its wiring changes, so an operator can tell
# which rule version produced a given held row. v1: composite median/MAD gate.
# v2: the same composite gate, byte-identical in decision, plus per-axis
# (``tool_mean`` / ``memory_mean``) robust z evidence and the opt-in per-axis
# hold behind ``per_axis_enforce``.
OUTLIER_ALGORITHM_VERSION = "outlier-escalation-v2"

# The score-payload axes the per-axis evidence covers, in recording order.
# ``composite = 0.6 * tool_mean + 0.4 * memory_mean`` (see ``ScoreReport``), so
# a gamed single axis can sit far out of band while the blend stays in band.
OUTLIER_AXES: tuple[str, ...] = ("tool_mean", "memory_mean")

# Ships OFF: per-axis outliers are observe-only evidence until an operator
# opts in AND the gate itself is in ``enforce`` mode.
DEFAULT_PER_AXIS_ENFORCE = False

# Accepted spellings for the boolean per-axis toggle. Anything else is
# rejected (``default_invalid_env``) and the shipped default stays in force.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


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

    ``per_axis_enforce`` lets a single-axis outlier (with the composite still
    at or above ``min_composite_floor``) hold on its own. It defaults OFF, so
    per-axis results are evidence only; even when on, it holds only in
    ``enforce`` mode and records a would-be hold in ``observe`` mode.
    """

    mode: str = "off"
    min_bench_version: int = DEFAULT_MIN_BENCH_VERSION
    min_cohort_size: int = DEFAULT_MIN_COHORT_SIZE
    modified_z_threshold: float = DEFAULT_MODIFIED_Z_THRESHOLD
    min_composite_floor: float = DEFAULT_MIN_COMPOSITE_FLOOR
    per_axis_enforce: bool = DEFAULT_PER_AXIS_ENFORCE


OUTLIER_ESCALATION_MODES: frozenset[str] = frozenset({"off", "observe", "enforce"})

# The environment variable behind each tunable. Operators read these names back
# through the admin posture endpoint; the values themselves are never echoed.
OUTLIER_ESCALATION_ENV_VARS: Mapping[str, str] = {
    "mode": "DITTO_OUTLIER_ESCALATION_MODE",
    "min_bench_version": "DITTO_OUTLIER_ESCALATION_MIN_BENCH_VERSION",
    "min_cohort_size": "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE",
    "modified_z_threshold": "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD",
    "min_composite_floor": "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR",
    "per_axis_enforce": "DITTO_OUTLIER_ESCALATION_PER_AXIS_ENFORCE",
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
    per_axis_enforce: OutlierSettingSource = "default"


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

    raw_per_axis = environ.get(env["per_axis_enforce"])
    per_axis_enforce_source: OutlierSettingSource
    if raw_per_axis is None:
        per_axis_enforce, per_axis_enforce_source = defaults.per_axis_enforce, "default"
    else:
        flag = raw_per_axis.strip().lower()
        if flag in _TRUE_VALUES:
            per_axis_enforce, per_axis_enforce_source = True, "env"
        elif flag in _FALSE_VALUES:
            per_axis_enforce, per_axis_enforce_source = False, "env"
        else:
            per_axis_enforce, per_axis_enforce_source = (
                defaults.per_axis_enforce,
                "default_invalid_env",
            )
    return OutlierEscalationSettingsLoad(
        settings=OutlierEscalationSettings(
            mode=mode,
            min_bench_version=min_bench_version,
            min_cohort_size=min_cohort_size,
            modified_z_threshold=modified_z_threshold,
            min_composite_floor=min_composite_floor,
            per_axis_enforce=per_axis_enforce,
        ),
        sources=OutlierEscalationSettingSources(
            mode=mode_source,
            min_bench_version=min_bench_version_source,
            min_cohort_size=min_cohort_size_source,
            modified_z_threshold=modified_z_threshold_source,
            min_composite_floor=min_composite_floor_source,
            per_axis_enforce=per_axis_enforce_source,
        ),
        loaded_at=now if now is not None else datetime.now(UTC),
    )


@dataclass(frozen=True)
class AxisObservation:
    """One score axis: the candidate's value and the same axis over its cohort.

    ``cohort`` is drawn from the same comparable peer set as the composite
    cohort (one value per peer), so every axis is judged against the same
    population the composite is.
    """

    value: float
    cohort: Sequence[float]


@dataclass(frozen=True)
class OutlierDecision:
    """Outcome of the outlier test for one finalized composite.

    ``held`` means the row is out-of-band and (in ``enforce`` mode) should be
    routed to ATH review instead of ranked. ``evidence`` is the immutable
    snapshot -- cohort stats and the candidate composite -- persisted on the
    review so the operator sees exactly WHY it held. It is always populated,
    even when ``held`` is False, so ``observe`` mode and the "insufficient data"
    branch leave an auditable record.

    ``axis_outliers`` names every axis whose own robust z is an upward
    out-of-band deviation, whether or not that axis contributed to ``held``.
    It is empty when no axes were supplied.
    """

    held: bool
    reason: str | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    axis_outliers: tuple[str, ...] = ()


def median_mad(values: Sequence[float]) -> tuple[float, float]:
    """Median centre and median absolute deviation of ``values``.

    Both are robust to a single extreme member, which is the whole point: a
    gamed spike must not be able to move the baseline it is measured against.
    """
    center = float(median(values))
    return center, float(median(abs(value - center) for value in values))


def _robust_deviation(
    value: float, peers: Sequence[float], threshold: float
) -> tuple[float, float, float | None, bool, bool]:
    """``(median, mad, modified_z, upward, beyond_threshold)`` of ``value``.

    The single statistic both the composite and every axis use. A zero-MAD
    cohort has no spread to divide by: any strictly-upward value counts as
    beyond the threshold and the modified z is ``None`` (JSON cannot carry
    an infinity).
    """
    center, mad = median_mad(peers)
    upward = value > center
    if mad == 0.0:
        return center, mad, None, upward, upward
    modified_z = _MAD_TO_SIGMA * (value - center) / mad
    return center, mad, modified_z, upward, modified_z >= threshold


def _axis_evidence(
    axis: str, observation: AxisObservation, settings: OutlierEscalationSettings
) -> dict[str, object]:
    """Per-axis robust z under the composite's exact cohort rules.

    Fails closed per axis: a cohort below ``min_cohort_size`` records
    ``anomaly_unavailable = "cohort_too_small"`` and is never an outlier.
    An axis is an outlier iff it is a strict upward deviation at or beyond
    ``modified_z_threshold`` -- the composite rule without the absolute
    floor, which stays a property of the composite (see the hold rule).
    """
    peers = [float(value) for value in observation.cohort]
    value = float(observation.value)
    # A fixed key set either way, so every recorded axis has the same shape.
    entry: dict[str, object] = {
        "axis": axis,
        "value": value,
        "cohort_size": len(peers),
        "cohort_median": None,
        "cohort_mad": None,
        "modified_z": None,
        "upward": None,
        "outlier": False,
        "anomaly_unavailable": None,
    }
    if len(peers) < settings.min_cohort_size:
        entry["anomaly_unavailable"] = "cohort_too_small"
        return entry
    center, mad, modified_z, upward, beyond = _robust_deviation(
        value, peers, settings.modified_z_threshold
    )
    entry.update(
        {
            "cohort_median": center,
            "cohort_mad": mad,
            "modified_z": modified_z,
            "upward": upward,
            "outlier": bool(beyond and upward),
        }
    )
    return entry


def evaluate_score_outlier(
    *,
    composite: float,
    cohort: Sequence[float],
    settings: OutlierEscalationSettings,
    axes: Mapping[str, AxisObservation] | None = None,
) -> OutlierDecision:
    """Decide whether ``composite`` is an out-of-band upward spike vs ``cohort``.

    ``cohort`` is the comparable same-benchmark peer set (the eligible ledger's
    composites), which already excludes the candidate itself, held agents, and
    banned agents. The candidate holds on the composite iff ALL of:

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

    ``axes`` (optional) adds per-axis evidence: each axis gets the same
    median/MAD robust z against its own cohort, recorded under ``per_axis``.
    Per-axis outliers are evidence only unless ``settings.per_axis_enforce``
    is on, in which case one also holds when the composite cohort is large
    enough and the composite is at or above ``min_composite_floor``. Whatever
    the axes say, the composite verdict and every composite evidence value are
    exactly what they are without ``axes``.
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

    axis_entries = (
        [
            _axis_evidence(axis, observation, settings)
            for axis, observation in axes.items()
        ]
        if axes is not None
        else None
    )
    axis_outliers = tuple(
        str(entry["axis"]) for entry in axis_entries or () if entry["outlier"]
    )

    def _decide(held: bool, trigger: str | None) -> OutlierDecision:
        if axis_entries is not None:
            evidence.update(
                {
                    "per_axis": axis_entries,
                    "per_axis_outlier_axes": list(axis_outliers),
                    "per_axis_enforce": settings.per_axis_enforce,
                    "trigger": trigger,
                }
            )
        # The reason is a fixed NEUTRAL string (no cohort numbers) because it
        # renders publicly; the statistics that justify the hold are in
        # ``evidence``, which only operators see. See ``OUTLIER_REVIEW_REASON``.
        return OutlierDecision(
            held=held,
            reason=OUTLIER_REVIEW_REASON if held else None,
            evidence=evidence,
            axis_outliers=axis_outliers,
        )

    # Fail closed on a thin cohort: too few points for a median/MAD to be a
    # meaningful baseline. Record the condition; never invent a decision.
    if cohort_size < settings.min_cohort_size:
        evidence["anomaly_unavailable"] = "cohort_too_small"
        return _decide(False, None)

    center, mad, modified_z, upward, beyond_distance = _robust_deviation(
        composite, peers, settings.modified_z_threshold
    )
    above_floor = composite >= settings.min_composite_floor

    evidence.update(
        {
            "cohort_median": center,
            "cohort_mad": mad,
            "modified_z": modified_z,
            "upward": upward,
            "above_floor": above_floor,
        }
    )

    if beyond_distance and upward and above_floor:
        return _decide(True, "composite")
    # Per-axis hold: opt-in, and still only for a ranks-threatening composite,
    # so a mediocre row with one odd axis is recorded but never parked.
    if settings.per_axis_enforce and axis_outliers and above_floor:
        return _decide(True, "per_axis")
    return _decide(False, None)


# The only per-axis fields the PUBLIC audit chain carries. ``score_audit_log``
# is served verbatim at ``/audit`` and every entry's hash covers its whole
# payload, so a field cannot be redacted at serialization without breaking
# chain verification -- it has to be left out when the entry is written.
_PUBLIC_AXIS_FIELDS = ("axis", "outlier")
_PRIVATE_EVIDENCE_KEYS = ("per_axis_outlier_axes", "per_axis_enforce")


def public_audit_evidence(evidence: Mapping[str, object]) -> dict[str, object]:
    """``evidence`` as written to the public, hash-chained audit log.

    Per-axis material is reduced to the neutral axis name and outlier flag:
    no per-axis value, cohort median/MAD, modified z or policy setting. The
    composite fields are passed through unchanged, so an entry without axes
    is exactly the evidence the gate has always published. The full snapshot
    stays operator-only: on ``ath_reviews.original_evidence`` for a hold, and
    recomputable through the admin dry-run replay.
    """
    public = {
        key: value
        for key, value in evidence.items()
        if key not in _PRIVATE_EVIDENCE_KEYS
    }
    per_axis = evidence.get("per_axis")
    if isinstance(per_axis, list):
        public["per_axis"] = [
            {name: entry.get(name) for name in _PUBLIC_AXIS_FIELDS}
            for entry in per_axis
            if isinstance(entry, Mapping)
        ]
    return public
