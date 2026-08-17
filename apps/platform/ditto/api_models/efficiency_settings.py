"""Versioned, hot-swappable operator settings for the relative token-efficiency
bonus (bench_version >= 7).

Where #403 baked the bonus toggles + knobs into boot-time env
(:class:`ditto.api_server.config.EfficiencyBonusConfig`), these wire models back
an append-only revision table so an operator can enable/disable/fold the bonus —
and retune every knob — live from backroom with no redeploy. The env config
remains the SEED DEFAULT applied when no revision has ever been written, so a
board with no revision is byte-identical to the pre-change behavior.

The numeric bounds mirror ``check_config`` exactly (see
``ditto.api_server.config.check_config``); the ``fold requires enabled``
invariant is enforced at READ time when the settings are overlaid onto the
config (``ditto.api_server.efficiency_settings.effective_config``), not here, so
it holds even for a directly-edited row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EfficiencyBonusSettings(BaseModel):
    """The full, hot-swappable efficiency-bonus policy — every value #403 read
    from ``DITTO_EFFICIENCY_BONUS_*``. Each revision stores the complete policy
    (not a diff) so a snapshot's frozen knobs are always reconstructable and a
    read never has to merge partial revisions.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    enabled: bool = False
    """Master switch. Gates cohort snapshotting, frozen bonus assignment, and
    public/validator exposure. Default off — identical to no revision."""

    fold_enabled: bool = False
    """Expose ``effective_composite`` on the validator ledger. Requires
    ``enabled``; the requirement is enforced at read time (a persisted
    ``fold_enabled=True`` with ``enabled=False`` folds nothing)."""

    cap: Annotated[float, Field(gt=0, le=0.10)] = 0.05
    """Tier-1 maximum bonus fraction at the P25 frontier (``0 < cap <= 0.10``)."""

    deep_cap: Annotated[float, Field(gt=0, le=0.10)] = 0.10
    """Tier-2 saturation cap (``cap <= deep_cap <= 0.10``)."""

    deep_frontier_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.5
    """Deep frontier as a fraction of P25, in ``(0, 1)``."""

    factor_alpha: Annotated[float, Field(gt=0, le=1)] = 0.25
    """Exponent for the bounded v3 cost ratio, in ``(0, 1]``."""

    minimum_factor: Annotated[float, Field(gt=0, le=1)] = 0.85
    """Lower clamp for a factor-curve envelope, in ``(0, 1]``.

    Curve v3 still applies this bound. Curve v4 records it for audit but
    does not clamp, so an operator can widen the stored envelope without a
    further code change.
    """

    maximum_factor: Annotated[float, Field(ge=1, le=100)] = 1.10
    """Upper clamp for a factor-curve envelope, in ``[1, 100]``.

    Curve v3 still applies this bound. Curve v4 is unclamped; a large
    stored maximum is how an operator documents that the envelope is off.
    """

    cohort_size: Annotated[int, Field(ge=2)] = 25
    """Top-N cap on cohort membership (``>= min_cohort``)."""

    min_cohort: Annotated[int, Field(ge=2)] = 8
    """Activation gate ``N_min`` (``>= 2``)."""

    epoch_hours: Annotated[int, Field(ge=1)] = 24
    """Length of one efficiency epoch in hours (``>= 1``)."""

    quality_floor: Annotated[float, Field(ge=0, le=1)] = 0.0
    """Static composite floor fallback, in ``[0, 1]``."""

    memory_floor: Annotated[float, Field(ge=0, le=1)] = 0.0
    """Static memory-mean floor fallback, in ``[0, 1]``."""

    @model_validator(mode="after")
    def validate_envelope(self) -> EfficiencyBonusSettings:
        """The cross-field bounds ``check_config`` enforces at boot."""
        if not self.cap <= self.deep_cap:
            raise ValueError("deep_cap must satisfy cap <= deep_cap <= 0.10")
        if self.minimum_factor > self.maximum_factor:
            raise ValueError("minimum_factor must not exceed maximum_factor")
        if self.cohort_size < self.min_cohort:
            raise ValueError("cohort_size must be at least min_cohort")
        return self


class EfficiencyBonusSettingsWrite(EfficiencyBonusSettings):
    """Whole-policy write shape with every policy field explicit.

    Read models keep defaults so revisions written before curve v3 remain
    usable. New writes must name every knob, preventing a partial or older
    client from silently resetting policy while changing an unrelated switch.
    """

    enabled: bool
    fold_enabled: bool
    cap: Annotated[float, Field(gt=0, le=0.10)]
    deep_cap: Annotated[float, Field(gt=0, le=0.10)]
    deep_frontier_ratio: Annotated[float, Field(gt=0, lt=1)]
    factor_alpha: Annotated[float, Field(gt=0, le=1)]
    minimum_factor: Annotated[float, Field(gt=0, le=1)]
    maximum_factor: Annotated[float, Field(ge=1, le=100)]
    cohort_size: Annotated[int, Field(ge=2)]
    min_cohort: Annotated[int, Field(ge=2)]
    epoch_hours: Annotated[int, Field(ge=1)]
    quality_floor: Annotated[float, Field(ge=0, le=1)]
    memory_floor: Annotated[float, Field(ge=0, le=1)]


class EfficiencyBonusSettingsRevision(BaseModel):
    """One append-only, operator-audited revision of the bonus policy."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: EfficiencyBonusSettings
    checksum_settings: dict[str, Any]
    """Exact immutable JSON payload hashed by ``checksum``. It may omit fields
    that did not exist when a historical revision was written; ``settings`` is
    the normalized/default-expanded compute view."""
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveEfficiencyBonusSettings(BaseModel):
    """What the compute path actually reads: the latest revision (or the env
    seed when none exists), plus its provenance for the operator console."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    """The governing revision number, or 0 when no revision exists (seed)."""

    scope: str
    settings: EfficiencyBonusSettings
    checksum_settings: dict[str, Any]
    checksum: str
    source: Annotated[str, Field(pattern="^(revision|seed)$")]
    """``revision`` when a stored revision governs, ``seed`` for the env
    default."""

    fold_effective: bool
    """The fold state actually in force after the read-time
    ``fold requires enabled`` clamp — may be ``False`` even when
    ``settings.fold_enabled`` is ``True`` (because ``enabled`` is off)."""

    max_age_seconds: float
    """The resolver TTL: an upper bound on how long a backroom change can take
    to land on the compute path (0 means every read re-reads)."""


class AdminEfficiencyBonusSettingsRequest(BaseModel):
    """Append one optimistic, confirmation-gated revision."""

    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    """Efficiency-bonus policy is subnet-global; only ``*`` is accepted."""

    expected_revision: Annotated[int, Field(ge=0)]
    """The revision the operator believes is current (0 = none yet). A
    mismatch is a 409 so a concurrent change is never silently clobbered."""

    settings: EfficiencyBonusSettingsWrite
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str
    """Must equal ``APPLY EFFICIENCY BONUS <ENABLED|DISABLED>`` (the resulting
    master-switch state), typed exactly."""


class AdminEfficiencyBonusSettingsResponse(BaseModel):
    """Current policy per scope, append-only history, the env seed, and the
    settings actually in force right now."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[EfficiencyBonusSettingsRevision]
    history: list[EfficiencyBonusSettingsRevision]
    seed_default: EfficiencyBonusSettings
    effective: EffectiveEfficiencyBonusSettings
