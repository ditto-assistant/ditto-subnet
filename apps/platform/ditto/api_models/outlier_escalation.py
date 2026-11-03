"""Operator read of the anomalous-score outlier escalation (issue #476).

The escalation is configured only by ``DITTO_OUTLIER_ESCALATION_*`` environment
variables read once per API process. This model reports the settings scoring is
actually using, where each value came from, and what the gate has recorded on
the append-only audit chain. The dry run replays the gate over the current
scored ledger. Both are read-only.

Not to be confused with ``/admin/score-outliers``, which is validator
disagreement inside one quorum.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

OutlierEscalationMode = Literal["off", "observe", "enforce"]
OutlierSettingSource = Literal["env", "default", "default_invalid_env"]
OutlierSettingField = Literal[
    "mode",
    "min_bench_version",
    "min_cohort_size",
    "modified_z_threshold",
    "min_composite_floor",
    "per_axis_enforce",
]

_SOURCE_DESCRIPTION = (
    "env: the variable was set and parsed. default: unset, shipped default. "
    "default_invalid_env: set but rejected, so the shipped default is in force."
)


_NON_FINITE = (
    "Null only when the environment set a non-finite value (nan/inf), which "
    "the loader accepts and JSON cannot carry. Scoring is using that value."
)


class OutlierEscalationSettingsView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    mode: OutlierEscalationMode
    min_bench_version: int
    min_cohort_size: int
    modified_z_threshold: Annotated[float | None, Field(description=_NON_FINITE)]
    min_composite_floor: Annotated[float | None, Field(description=_NON_FINITE)]
    per_axis_enforce: Annotated[
        bool,
        Field(
            description=(
                "Whether a single-axis outlier may hold on its own (only in "
                "enforce mode, and only with the composite at or above "
                "min_composite_floor). Off: per-axis results are evidence only."
            )
        ),
    ]


class OutlierEscalationSettingSourcesView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    mode: Annotated[OutlierSettingSource, Field(description=_SOURCE_DESCRIPTION)]
    min_bench_version: OutlierSettingSource
    min_cohort_size: OutlierSettingSource
    modified_z_threshold: OutlierSettingSource
    min_composite_floor: OutlierSettingSource
    per_axis_enforce: OutlierSettingSource


class OutlierEscalationAxisEvidence(BaseModel):
    """One score axis's robust z against its cohort; null when absent."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    axis: Annotated[str, Field(description="Score axis, e.g. tool_mean.")]
    value: float | None = None
    cohort_size: int | None = None
    cohort_median: float | None = None
    cohort_mad: float | None = None
    modified_z: Annotated[
        float | None,
        Field(
            default=None,
            description="Null for a zero-MAD or too-small axis cohort.",
        ),
    ]
    upward: bool | None = None
    outlier: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Strict upward deviation at or beyond modified_z_threshold. "
                "Always false when the axis cohort is too small."
            ),
        ),
    ]
    anomaly_unavailable: Annotated[
        str | None,
        Field(
            default=None,
            description="cohort_too_small when the axis failed closed.",
        ),
    ]


class OutlierEscalationEvidence(BaseModel):
    """Scalar evidence the escalation recorded; null when absent or mistyped."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    composite: float | None = None
    cohort_size: int | None = None
    cohort_median: float | None = None
    cohort_mad: float | None = None
    modified_z: Annotated[
        float | None,
        Field(
            default=None,
            description="Null for a zero-MAD cohort (no spread to divide by).",
        ),
    ]
    min_cohort_size: int | None = None
    modified_z_threshold: float | None = None
    min_composite_floor: float | None = None
    upward: bool | None = None
    above_floor: bool | None = None
    per_axis: Annotated[
        list[OutlierEscalationAxisEvidence] | None,
        Field(
            default=None,
            description=(
                "Per-axis robust z evidence (outlier-escalation-v2+). Null on "
                "entries recorded before per-axis evidence existed. Audit-chain "
                "entries are public and record only axis and outlier; their "
                "statistics are null (the dry run reports them in full)."
            ),
        ),
    ]
    per_axis_outlier_axes: list[str] | None = None
    per_axis_enforce: bool | None = None
    trigger: Annotated[
        Literal["composite", "per_axis"] | None,
        Field(
            default=None,
            description=(
                "What held (or would hold) the row. Null for evidence-only "
                "per-axis entries and for pre-v2 entries."
            ),
        ),
    ]


class OutlierEscalationEntryView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    seq: Annotated[int, Field(ge=1, description="score_audit_log append order.")]
    agent_id: UUID
    recorded_at: datetime
    enforced: Annotated[
        bool,
        Field(
            description=(
                "true: an ATH hold was opened (enforce). false: a would-be hold "
                "was only recorded (observe)."
            )
        ),
    ]
    bench_version: int | None = None
    algorithm_version: str | None = None
    evidence: OutlierEscalationEvidence


class OutlierEscalationActivityView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    window_hours: Annotated[int, Field(ge=1)]
    window_started_at: datetime
    observed_total: Annotated[int, Field(ge=0)]
    enforced_total: Annotated[int, Field(ge=0)]
    observed_in_window: Annotated[int, Field(ge=0)]
    enforced_in_window: Annotated[int, Field(ge=0)]
    axis_evidence_total: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Evidence-only per-axis outlier entries (audit_kind "
                "anomalous_score_axis): an axis out of band while the policy "
                "held nothing. Never counted as observed or enforced."
            ),
        ),
    ] = 0
    axis_evidence_in_window: Annotated[int, Field(ge=0)] = 0
    latest_recorded_at: datetime | None = None
    recent_limit: Annotated[int, Field(ge=1)]
    recent: list[OutlierEscalationEntryView]
    recent_truncated: Annotated[
        bool,
        Field(description="More matching entries exist beyond recent_limit."),
    ]
    recent_axis_evidence: Annotated[
        list[OutlierEscalationEntryView],
        Field(
            default_factory=list,
            description=(
                "Newest evidence-only per-axis entries, at most recent_limit "
                "rows. enforced is always false."
            ),
        ),
    ]
    recent_axis_evidence_truncated: bool = False


class AdminOutlierEscalationResponse(BaseModel):
    """Effective posture, per-field sources, and audit-chain activity."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    generated_at: datetime
    settings_loaded_at: Annotated[
        datetime,
        Field(
            description=(
                "When this API process read the environment. Settings change "
                "only on a process restart."
            )
        ),
    ]
    settings: OutlierEscalationSettingsView
    defaults: OutlierEscalationSettingsView
    sources: OutlierEscalationSettingSourcesView
    env_vars: Annotated[
        dict[str, str],
        Field(description="Environment variable name per setting (names only)."),
    ]
    invalid_env_fields: list[OutlierSettingField]
    review_kind: str
    algorithm_version: str
    pending_review_count: Annotated[
        int,
        Field(ge=0, description="Pending ATH reviews opened as anomalous_score."),
    ]
    activity: OutlierEscalationActivityView


class OutlierEscalationDryRunEntryView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    miner_hotkey: str
    evidence: OutlierEscalationEvidence


class AdminOutlierEscalationDryRunResponse(BaseModel):
    """Would-trigger replay of the escalation over the current scored ledger.

    Mode-independent and read-only: it opens no hold, writes no audit entry,
    and changes no setting.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    generated_at: datetime
    bench_version: int
    bench_version_in_scope: Annotated[
        bool,
        Field(
            description=(
                "bench_version >= settings.min_bench_version. When false the "
                "live gate never runs at this version; counts are still replayed."
            )
        ),
    ]
    settings: Annotated[
        OutlierEscalationSettingsView,
        Field(
            description=(
                "The policy replayed: the effective settings with any override "
                "applied. mode is reported, not applied."
            )
        ),
    ]
    overridden_fields: list[OutlierSettingField]
    ledger_size: Annotated[
        int,
        Field(ge=0, description="Candidates replayed: one per ledger owner."),
    ]
    cohort_size: Annotated[
        int,
        Field(
            ge=0,
            description="Peers per candidate: the ledger without the candidate.",
        ),
    ]
    cohort_too_small: Annotated[
        bool,
        Field(description="cohort_size < min_cohort_size, so nothing can trigger."),
    ]
    ledger_median: Annotated[
        float | None,
        Field(
            description=(
                "Median of all ledger composites (null when empty). Each "
                "entry's evidence carries its own leave-one-out median/MAD."
            )
        ),
    ] = None
    ledger_mad: float | None = None
    would_trigger_count: Annotated[int, Field(ge=0)]
    axis_evidence_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Rows with a per-axis outlier that the replayed policy would "
                "NOT hold (recorded as evidence only by the live gate)."
            ),
        ),
    ] = 0
    limit: Annotated[int, Field(ge=1)]
    would_trigger: Annotated[
        list[OutlierEscalationDryRunEntryView],
        Field(description="Highest composite first, at most limit rows."),
    ]
    truncated: Annotated[
        bool,
        Field(description="would_trigger_count exceeds the returned rows."),
    ]
    axis_evidence: Annotated[
        list[OutlierEscalationDryRunEntryView],
        Field(
            default_factory=list,
            description="Highest composite first, at most limit rows.",
        ),
    ]
    axis_evidence_truncated: bool = False
