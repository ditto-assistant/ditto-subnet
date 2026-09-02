"""Public, unauthenticated read models for the subnet dashboard.

These expose the **aggregate** shape only: composite plus tool/memory means and
rank, and deliberately omit the fields on :class:`LedgerEntry` that are either
integrity-internal (``sha256``, ``signature``, ``validator_hotkey``) or would
hand a miner the benchmark's answer key (per-case ``expected``/``called``). See
``docs/public-telemetry.md`` for the transparency policy this encodes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.benchmark_capacity import BenchmarkAdmission
from ditto.api_models.benchmark_progress import BenchmarkProgressStage
from ditto.api_models.confirmation_progress import ConfirmationProgressStage
from ditto.api_models.name_claim import PublicNameHandle
from ditto.api_models.retry_state import RetryState
from ditto.api_models.screener import ScreenerProgressStage, ScreenerRuntimeState
from ditto.api_models.stack_health import ValidatorStackHealth
from ditto.api_models.ticket_status import TicketPurpose
from ditto.api_models.validator import (
    V9ConfirmationReceipt,
    ValidatorRuntimeState,
)
from ditto.api_models.validator_capabilities import (
    ValidatorCapabilities,
    ValidatorStackIdentity,
)
from ditto.api_models.validator_updater import ValidatorUpdaterStatus
from ditto_screening_protocol.bench_v9 import V9EvidenceBenchVersion

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"

# Family-member DISPLAY models accept a zero composite; ranking models do not.
#
# A legacy bench-v6 child that genuinely scored 0.0 is valid history, and the
# leaderboard renders it in the owner's family dropdown. Excluding zero here was
# not a policy choice about emissions -- it made the whole response unserializable,
# so `GET /api/v1/public/leaderboard?bench_version=6` returned 500 for every
# caller whenever any owner family contained such a child.
#
# This bound governs rendering only. Zero-score rows stay unranked and
# provisional, cannot displace a positive eligible score as the owner
# representative, and never enter KOTH/tail allocation or validator weights --
# those gates live in the eligibility and ranking paths, not in a display bound.
# Non-finite values remain rejected: NaN and +inf fail `le`, -inf fails `ge`.
_MIN_DISPLAY_COMPOSITE = 0.0


class PublicBenchmarkRelease(BaseModel):
    """One immutable DittoBench contract release shown on the timeline."""

    bench_version: Annotated[int, Field(ge=1)]
    released_at: datetime
    activated_at: datetime | None = None
    title: str


class PublicBenchmarkTimelinePoint(BaseModel):
    """A new best finalized miner memory median within one benchmark era."""

    recorded_at: datetime
    bench_version: Annotated[int, Field(ge=1)]
    agent_id: UUID
    agent_name: str
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    memory_mean: Annotated[float, Field(ge=0.0, le=1.0)]
    composite: Annotated[float, Field(gt=0.0, le=1.0)]
    score_count: Annotated[int, Field(ge=3)]


class PublicBenchmarkTimelineResponse(BaseModel):
    """Historical top-miner memory progress and benchmark release events."""

    generated_at: datetime
    metric: Literal["memory_mean"] = "memory_mean"
    score_quorum: Annotated[int, Field(ge=1)]
    releases: list[PublicBenchmarkRelease]
    points: list[PublicBenchmarkTimelinePoint]


class PublicCategoryStat(BaseModel):
    """One category's mean in a run's per-category breakdown (public)."""

    category: Annotated[
        str, Field(description="Category name (tool name / memory type).")
    ]
    count: Annotated[int, Field(ge=0, description="Cases scored in this category.")]
    mean: Annotated[float, Field(ge=0.0, le=1.0, description="Mean score in [0,1].")]


class PublicBenchIntegrity(BaseModel):
    """Anti-overfit / scoring-integrity telemetry for a scored run (public).

    These describe *how the dataset resists gaming*, not the miner's answers:
    the paraphrase pass (reword-or-fallback), the NoLiMa lexical-gap rewrite
    (questions reworded to share fewer content words with the stored fact), how
    many tool cases were capped because the harness self-reported instead of
    calling the observable endpoint, and the memory seeding-wave count. They are
    uniform across miners scored on the same seed/version and exist so the
    community can audit the benchmark's anti-overfit posture.
    """

    paraphrase_applied: Annotated[
        int | None,
        Field(default=None, ge=0, description="Cases whose text was paraphrased."),
    ]
    paraphrase_attempted: Annotated[
        int | None,
        Field(default=None, ge=0, description="Cases the paraphraser was run on."),
    ]
    paraphrase_fallback: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Paraphrases that failed verify and fell back to template.",
        ),
    ]
    lexical_gap_rewritten: Annotated[
        int | None,
        Field(default=None, ge=0, description="Questions reworded to drop a word."),
    ]
    lexical_gap_questions: Annotated[
        int | None,
        Field(default=None, ge=0, description="Questions considered for lexical gap."),
    ]
    lexical_gap_mean_before: Annotated[
        float | None,
        Field(default=None, ge=0.0, description="Mean shared-content overlap before."),
    ]
    lexical_gap_mean_after: Annotated[
        float | None,
        Field(default=None, ge=0.0, description="Mean shared-content overlap after."),
    ]
    capped_tool_cases: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Tool cases capped (self-report untrusted, not via endpoint).",
        ),
    ]
    seeding_waves: Annotated[
        int | None,
        Field(default=None, ge=0, description="Memory seeding waves in this run."),
    ]


class PublicCaseResult(BaseModel):
    """One scored case, **redacted** for public per-case analysis.

    Carries only *how the agent did* on the case: its category, kind, score,
    pass/fail, latency, and the scorer's mechanical notes (e.g. "1 extra tool
    call", "capped: self-report untrusted"). It deliberately **omits the answer
    key**: the ``expected`` tools/answer, the agent's ``called`` tools (which on a
    correct case would reveal ``expected``), and the seed-derived ``case_id``.
    Combined with per-submission seed rotation, this lets anyone inspect a run's
    per-case strengths/weaknesses without learning anything that helps overfit.

    ``notes`` is drawn from a **closed vocabulary** of mechanical verdicts. The
    scorers themselves interpolate dataset content into some notes (the matched
    distractor value, required/forbidden argument names); the public projection
    rebuilds each note from validated primitives and drops anything it does not
    recognize, so the value a note was rendered around never reaches the wire.
    """

    category: Annotated[
        str, Field(description="Case category (tool name / memory question type).")
    ]
    kind: Annotated[str, Field(description='"tool" or "memory".')]
    score: Annotated[float, Field(ge=0.0, le=1.0, description="Case score in [0,1].")]
    correct: Annotated[
        bool | None, Field(default=None, description="Whether the case passed.")
    ]
    latency_ms: Annotated[
        int | None, Field(default=None, ge=0, description="Case latency (ms).")
    ]
    notes: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Scorer's mechanical notes, from a closed vocabulary (no answers, "
                "no dataset values, no agent-supplied text)."
            ),
        ),
    ]


class PublicRunModels(BaseModel):
    """The LLM models a scored run was produced with (public transparency)."""

    generator: Annotated[
        str | None, Field(default=None, description="Datagen model id.")
    ]
    judge: Annotated[
        str | None, Field(default=None, description="Judge/scorer model id.")
    ]
    judge_audit: Annotated[
        str | None,
        Field(default=None, description="Second (audit) judge model id, if any."),
    ]
    harness: Annotated[
        str | None,
        Field(
            default=None,
            description="Miner harness chat model id, when the operator pinned it.",
        ),
    ]


class PublicTokenUsage(BaseModel):
    """Trusted provider usage observed by the validator-owned model proxy."""

    accounting_version: Annotated[int, Field(ge=1)]
    status: Annotated[str, Field(pattern=r"^(complete|unavailable)$")]
    source: str
    provider: str
    profile_revision: str
    model: str
    prompt_tokens: Annotated[int, Field(ge=0)]
    prompt_bytes: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    total_tokens: Annotated[int, Field(ge=0)]
    requests: Annotated[int, Field(ge=0)]
    successes: Annotated[int, Field(ge=0)]
    usage_available: Annotated[int, Field(ge=0)]
    usage_unavailable: Annotated[int, Field(ge=0)]
    provider_latency_ms: Annotated[int, Field(ge=0)]
    ttft_status: str


class PublicModelUse(BaseModel):
    """Whether a scored run actually used the language model, and why.

    Published to every miner. Deliberately an allowlist of counts, ratios and
    the verdict -- no prompts, no completions, no case content, no source.
    Nothing here can be inverted toward another miner's implementation, which
    is what makes across-the-board observability compatible with the anti-copy
    posture in ditto-platform#375.

    The thresholds ride along with the verdict on purpose: a miner must be able
    to read the bar off the same payload that reports their standing against
    it, so the published rule and the enforced rule can never drift.
    """

    verdict: Annotated[str, Field(pattern=r"^(unmeasured|used|not_used)$")]
    calls: Annotated[int | None, Field(default=None, ge=0)]
    prompt_tokens: Annotated[int | None, Field(default=None, ge=0)]
    completion_tokens: Annotated[int | None, Field(default=None, ge=0)]
    cases: Annotated[int | None, Field(default=None, ge=0)]
    prompt_tokens_per_case: Annotated[float | None, Field(default=None, ge=0.0)]
    calls_per_case: Annotated[float | None, Field(default=None, ge=0.0)]
    prompt_tokens_per_call: Annotated[float | None, Field(default=None, ge=0.0)]
    reason: str | None = None
    min_prompt_tokens_per_case: Annotated[float | None, Field(default=None, ge=0.0)]
    min_calls_per_case: Annotated[float | None, Field(default=None, ge=0.0)]
    min_prompt_tokens_per_call: Annotated[float | None, Field(default=None, ge=0.0)]


PublicV9GateResult = Literal[
    "passed",
    "below_threshold",
    "zero_inference",
    "insufficient_evidence",
    "not_applicable",
]


class PublicV9ModelUseGate(BaseModel):
    """Allowlisted trusted-relay counts behind the v9 model-use gate.

    The full signature-bound root also carries token counts, exclusion reasons,
    contract digests, and artifact identities. Those stay out of the public
    dashboard payload: this view contains only the aggregate counts and frozen
    threshold needed to explain the displayed gate result.
    """

    administered_cases: Annotated[int, Field(ge=0)]
    eligible_cases: Annotated[int, Field(ge=0)]
    successful_inference_cases: Annotated[int, Field(ge=0)]
    missing_inference_cases: Annotated[int, Field(ge=0)]
    observed_requests: Annotated[int, Field(ge=0)]
    successful_requests: Annotated[int, Field(ge=0)]
    request_coverage_bps: Annotated[int, Field(ge=0, le=10_000)]
    coverage_bps: Annotated[int, Field(ge=0, le=10_000)]
    threshold_bps: Annotated[int, Field(ge=1, le=10_000)]
    result: PublicV9GateResult
    factor_bps: Literal[0, 10000]


class PublicV9AuthoritativeToolGate(BaseModel):
    """Allowlisted trusted tool-server counts behind the v9 tool gate."""

    expected_executions: Annotated[int, Field(ge=0)]
    matched_executions: Annotated[int, Field(ge=0)]
    missing_executions: Annotated[int, Field(ge=0)]
    unexpected_executions: Annotated[int, Field(ge=0)]
    observed_executions: Annotated[int, Field(ge=0)]
    coverage_bps: Annotated[int, Field(ge=0, le=10_000)]
    threshold_bps: Annotated[int, Field(ge=1, le=10_000)]
    result: PublicV9GateResult
    factor_bps: Literal[0, 10000]


class PublicV9ScoreGateEvidence(BaseModel):
    """Privacy-safe public projection of the signed v9 score gates."""

    rollout_mode: Literal["shadow", "enforce"]
    model_use: PublicV9ModelUseGate
    authoritative_tool: PublicV9AuthoritativeToolGate


class PublicV9BaseEvidence(BaseModel):
    """Public v9 evidence consumed by the dashboard's score drill-downs.

    ``bench_version`` pins the shared protocol alias, never its own literal: the
    signed evidence this projects already accepts every epoch the stack was
    carried forward to, so restating the set here is how a v10 score became a
    500 on ``/agent/{id}/scores``.
    """

    bench_version: V9EvidenceBenchVersion
    score_gates: PublicV9ScoreGateEvidence


class PublicTokenEfficiency(BaseModel):
    """Auditable v5 relay-token waste penalty."""

    formula_version: str
    baseline_id: str | None = None
    baseline_prompt_tokens: Annotated[int | None, Field(default=None, ge=0)]
    baseline_completion_tokens: Annotated[int | None, Field(default=None, ge=0)]
    baseline_total_tokens: Annotated[int | None, Field(default=None, ge=0)]
    budget_percentile: Annotated[float, Field(gt=0.0, le=1.0)]
    observed_prompt_tokens: Annotated[int, Field(ge=0)]
    observed_completion_tokens: Annotated[int, Field(ge=0)]
    observed_total_tokens: Annotated[int, Field(ge=0)]
    excess_ratio: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    maximum_penalty: Annotated[float, Field(ge=0.0, le=1.0)]
    minimum_multiplier: Annotated[float, Field(ge=0.0, le=1.0)]
    multiplier: Annotated[float, Field(ge=0.9, le=1.0)]
    raw_composite: Annotated[float, Field(ge=0.0, le=1.0)]
    adjusted_composite: Annotated[float, Field(ge=0.0, le=1.0)]
    penalty_applied: bool
    decision_reason: str


class PublicBenchmarkQualityFactor(BaseModel):
    """One public-safe input to the scorer-owned benchmark quality multiplier."""

    key: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    label: str
    metric: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = None
    multiplier: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = None
    audit_count: Annotated[int | None, Field(default=None, ge=0)] = None
    explanation: str


class PublicCompositeBreakdown(BaseModel):
    """Public arithmetic from capability means to the final composite.

    The scorer remains authoritative for every multiplier. ``quality_factors``
    is an allowlisted projection of stored aggregate telemetry; it never
    reconstructs case content or reimplements benchmark policy.
    """

    formula: str = (
        "(0.5 * tool_mean + 0.5 * memory_mean) * "
        "benchmark_quality_multiplier * token_efficiency_multiplier"
    )
    tool_weight: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    memory_weight: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    base_accuracy: Annotated[float, Field(ge=0.0, le=1.0)]
    benchmark_quality_multiplier: Annotated[float, Field(ge=0.0, le=1.0)]
    quality_factors: list[PublicBenchmarkQualityFactor] = Field(default_factory=list)
    pre_token_composite: Annotated[float, Field(ge=0.0, le=1.0)]
    token_efficiency_multiplier: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.9,
            le=1.0,
            description=(
                "Benchmark-v5 token multiplier; null when token efficiency does "
                "not apply or was unavailable."
            ),
        ),
    ] = None
    token_penalty: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=0.1,
            description="Fraction removed by token efficiency; capped at 10%.",
        ),
    ] = None
    maximum_token_penalty: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=0.1),
    ] = None
    final_composite: Annotated[float, Field(ge=0.0, le=1.0)]


class PublicArtifactRelease(BaseModel):
    """Public-source eligibility derived from a submission's score quorum."""

    status: Literal[
        "awaiting_quorum",
        "under_review",
        "embargoed",
        "available",
        "unavailable",
        "withheld",
    ]
    """Where this submission sits on the release ladder.

    ``withheld`` is the only value that does not describe a stage of the
    release process: subnet policy is ``disclosure = never``, so no submission
    is published at all. It is deliberately distinct from ``unavailable``,
    which means "not eligible under the king-only rule" and flips to
    ``embargoed`` the moment that submission is crowned. A client that
    conflates the two shows a withheld submission as merely un-crowned and
    implies it could still be published by winning.
    """

    disclosure: Annotated[
        Literal["public", "never"],
        Field(
            default="public",
            description=(
                "Subnet-wide source-disclosure policy, identical for every "
                "submission. 'never' means no source is published at all; the "
                "screener, the k=3 validators and operator copy review still "
                "read it, so submissions are scored and plagiarism-checked "
                "exactly as before."
            ),
        ),
    ] = "public"
    bench_version: Annotated[int | None, Field(default=None, ge=1)] = None
    score_quorum: Annotated[int, Field(default=3, ge=1)] = 3
    embargo_hours: Annotated[int, Field(default=6, ge=1)] = 6
    finalized_at: datetime | None = None
    crowned_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "When this agent first became the KOTH king (eligibility marker). "
                "Null for a submission that has never held the crown."
            ),
        ),
    ] = None
    weight_confirmed_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "When validators' revealed on-chain weights (post commit-reveal) "
                "were first seen set on this king. Source release is king-only and "
                "the embargo window is measured from this instant; null while a "
                "king still awaits on-chain confirmation."
            ),
        ),
    ] = None
    available_at: datetime | None = None
    download_available: bool = False


class PublicArtifactDownload(BaseModel):
    """Short-lived download credential for an embargo-cleared source tarball."""

    agent_id: UUID
    bench_version: Annotated[int, Field(ge=1)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    finalized_at: datetime
    download_url: Annotated[str, Field(min_length=1)]
    expires_at: datetime


class PublicSubmissionFamilyMember(BaseModel):
    """One finalized submission sharing a leaderboard ownership slot."""

    agent_id: UUID
    agent_name: str
    agent_version: Annotated[int | None, Field(default=None, ge=1)] = None
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    canonical_composite: Annotated[float, Field(ge=_MIN_DISPLAY_COMPOSITE, le=1.0)]
    submitted_at: datetime
    representative: bool = False


class PublicSubmissionFamily(BaseModel):
    """The scored generations collapsed into one owner leaderboard position."""

    member_count: Annotated[int, Field(ge=1)]
    selection_rule: Literal[
        "best_official_score_per_payment_owner",
        "best_canonical_score_per_payment_owner",
    ] = "best_official_score_per_payment_owner"
    members: list[PublicSubmissionFamilyMember] = Field(default_factory=list)


class PublicLeaderboardFamilyMember(BaseModel):
    """One compact, unranked child in a leaderboard owner grouping."""

    agent_id: UUID
    agent_name: str
    agent_version: Annotated[int | None, Field(default=None, ge=1)] = None
    canonical_composite: Annotated[float, Field(ge=_MIN_DISPLAY_COMPOSITE, le=1.0)]
    submitted_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "When this generation arrived (UTC). The family's earliest "
                "arrival within the dethrone margin of the winner is what the "
                "KOTH fold orders on, so this is what lets a reader locate the "
                "generation supplying the winner's ``crown_first_seen`` rather "
                "than infer it."
            ),
        ),
    ] = None
    miner_hotkey: Annotated[
        str | None,
        Field(
            default=None,
            pattern=_SS58_PATTERN,
            description=(
                "This generation's own hotkey, which need not be the winner's. "
                "Owner families are resolved across attested payment roots, so "
                "crown seniority can be inherited from a sibling on a different "
                "hotkey. That is legitimate and previously invisible."
            ),
        ),
    ] = None
    confirmation_seed_depth: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            exclude_if=lambda value: value == 0,
            description=(
                "Raw count of distinct continual-retest seeds accepted for this "
                "submission. Omitted at zero to keep compact family rows lean."
            ),
        ),
    ] = 0


class PublicLeaderboardFamily(BaseModel):
    """Only the grouped children needed by the expandable leaderboard row."""

    members: list[PublicLeaderboardFamilyMember] = Field(default_factory=list)


class PublicLeaderboardEntry(BaseModel):
    """One miner's best score, aggregate-only, for public display.

    Beyond the headline composite + tool/memory means, this carries the
    benchmark provenance a transparent leaderboard needs: the models that
    generated + graded the run, the ``bench_version`` and ``dataset_sha256``
    (which pins the exact scored artifact for a dispute re-score), latency, case
    count, and a per-category breakdown. All are advisory and deliberately
    exclude the raw ``seed`` (anti-overfit) and any per-case answer-key content
    (``expected`` / ``called``).
    """

    rank: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "1-based rank by ``official_composite`` -- NOT by ``composite``. "
                "Sorting this board by ``composite`` reproduces ``rank`` only "
                "while every entry is still on the canonical median "
                "(``aggregate_method == 'canonical_median'``); once any agent "
                "has completed continual cohort waves the two orderings differ, "
                "and ``official_composite`` is the authoritative quality that "
                "ranks the board and drives the weight fold. Bench-v9 curve-v3 "
                "efficiency breaks only exact official-quality ties; lower "
                "quality never crosses higher quality. Remaining ties break on "
                "``crown_first_seen`` (the lineage arrival; falling back to "
                "``first_seen``) then ``agent_id``. A later tarball that only "
                "matches or improves the owner's best score by less than the "
                "dethrone margin keeps that earlier clock; a jump larger than "
                "that margin resets it. Provisional rows are ranked among "
                "themselves and always trail the finalized board. Bench v9 "
                "base/provisional rows in confirmation enforce mode are null: "
                "only full-confirmed rows rank."
            ),
        ),
    ] = None
    finalized: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "Whether the submission reached the three-validator quorum. "
                "False entries are provisional feedback and never drive weights."
            ),
        ),
    ]
    score_count: Annotated[
        int,
        Field(
            default=3,
            ge=1,
            description="Accepted independent validator scores currently available.",
        ),
    ]
    score_quorum: Annotated[
        int,
        Field(default=3, ge=1, description="Scores required for finalization."),
    ]
    average_run_cost_microusd: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Mean platform-metered chat plus embedding spend for this "
                "agent's completed, non-empty validator leases on the displayed "
                "benchmark version. A lease counts only once the validator "
                "holding it posted a score, so a run a stalled validator "
                "abandoned is excluded rather than averaged in as cheap work. "
                "Restricted to the current metering contract, since totals are "
                "not comparable across a meter change. Null when the retained "
                "run ledger holds no completed samples -- which is normal for "
                "an agent whose quorum has not finished scoring yet."
            ),
        ),
    ] = None
    inference_run_count: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description=(
                "Completed validator leases included in "
                "average_run_cost_microusd. Expect single digits: this counts "
                "scored leases, not every lease the agent was ever issued."
            ),
        ),
    ] = 0
    agent_id: Annotated[
        UUID,
        Field(
            description=(
                "The scored agent's id, to drill into its k=3 record at "
                "/public/agent/{id}/scores. Already public via "
                "/public/submissions."
            )
        ),
    ]
    agent_name: Annotated[
        str,
        Field(description="Human-friendly name of the miner's winning agent."),
    ]
    avatar_url: str | None = Field(
        default=None,
        description="Public URL for this miner's signed profile picture, if set.",
    )
    name_handle: PublicNameHandle | None = Field(
        default=None,
        description=(
            "Signed handle reservation touching this name, when one exists. "
            "``reserved`` means this owner family holds the stem; ``disputed`` "
            "means an upheld reservation belongs to someone else; ``pending`` "
            "means a claim is awaiting entrenched-miner endorsements."
        ),
    )
    agent_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description="Winning submission's version; null for legacy uploads.",
        ),
    ] = None
    artifact_release: PublicArtifactRelease | None = None
    submission_family: PublicLeaderboardFamily | None = Field(
        default=None,
        description=(
            "Compact finalized children sharing this entry's owner slot. Only "
            "identity/version and canonical score are included; full family "
            "evidence is loaded from the agent detail endpoint."
        ),
    )
    miner_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Miner's SS58 hotkey.")
    ]
    miner_uid: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Miner's current UID on this subnet; null when the hotkey is "
                "not registered or the chain snapshot is unavailable."
            ),
        ),
    ] = None
    registered: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Whether the miner hotkey currently has a UID on this subnet. "
                "False pauses weight and emission eligibility without deleting "
                "the submission or score; null means the chain snapshot was "
                "temporarily unavailable."
            ),
        ),
    ]
    emission_eligible: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Whether this entry is finalized on the current benchmark, "
                "full-benchmark eligible, and currently registered, so validators "
                "may include it in the active weight fold. Null when registration "
                "could not be read."
            ),
        ),
    ]
    composite: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description=(
                "Canonical three-validator median in [0,1]. Preserved for score "
                "provenance; use official_composite for current ranking."
            ),
        ),
    ]
    official_composite: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.1,
            description=(
                "Authoritative quality used as the primary key for the current "
                "leaderboard and weight fold. "
                "Legacy eras use the canonical median or activated continual "
                "mean; full-confirmed Bench v9 uses its verified full quality. "
                "Historical bonuses remain in this scalar. A v9 curve-v3 "
                "factor does not modify it; its adjusted projection is only an "
                "exact-quality secondary key."
            ),
        ),
    ]
    v9_confirmation_status: Annotated[
        Literal["base_only", "provisional", "full_confirmed"] | None,
        Field(
            default=None,
            exclude_if=lambda value: value is None,
            description=(
                "Bench v9 score contract state. Base-only/provisional values are "
                "never ranked in enforce mode; full_confirmed is the only "
                "reward-authoritative state."
            ),
        ),
    ] = None
    v9_full_confirmed_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            exclude_if=lambda value: value is None,
            description=(
                "Independently derivable full v9 composite used for this rank. "
                "Null for base-only and shadow/provisional rows."
            ),
        ),
    ] = None
    v9_shadow_quality_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            exclude_if=lambda value: value is None,
            description=(
                "Display-only 70/30 mix from a completed shadow confirmation. "
                "Never ranked or weighted while mode is shadow."
            ),
        ),
    ] = None
    v9_longmem_mean_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            exclude_if=lambda value: value is None,
            description=(
                "Display-only LongMemEval mean from completed confirmation "
                "evidence. Independent of ranking authority."
            ),
        ),
    ] = None
    v9_confirmation_evidence_sha256: Annotated[
        str | None,
        Field(
            default=None,
            pattern=r"^[0-9a-f]{64}$",
            exclude_if=lambda value: value is None,
            description="Signed full-confirmation evidence root digest.",
        ),
    ] = None
    aggregate_method: Literal["canonical_median", "continual_mean"] = "canonical_median"
    pre_efficiency_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "The authoritative quality composite before the frozen "
                "efficiency bonus or factor. Equal to official_composite while "
                "the adjustment fold is inactive."
            ),
        ),
    ] = None
    aggregate_sample_count: Annotated[int, Field(ge=3)] = 3
    completed_wave_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Compatibility alias for retained_sample_count. Historically "
                "this was a current-cohort intersection; it now counts the "
                "accepted per-seed samples actually used by this agent's mean."
            ),
        ),
    ] = 0
    retained_sample_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Accepted per-seed samples permanently included in this "
                "agent's continual mean."
            ),
        ),
    ] = 0
    initial_quorum_composites: list[Annotated[float, Field(ge=0.0, le=1.0)]] = Field(
        default_factory=list
    )
    completed_wave_composites: list[Annotated[float, Field(ge=0.0, le=1.0)]] = Field(
        default_factory=list
    )
    confirmation_seed_depth: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Raw count of distinct seeds this submission has accepted "
                "continual-retest evidence for. This is append-only and equals "
                "retained_sample_count while the continual aggregate is active."
            ),
        ),
    ] = 0
    confirmation_seed_composites: list[Annotated[float, Field(ge=0.0, le=1.0)]] = Field(
        default_factory=list,
        description=(
            "Per-seed median confirmation composites behind "
            "``confirmation_seed_depth``, ordered by seed. Audit-only: these "
            "never enter ``official_composite`` unless their seed also appears "
            "in ``completed_wave_composites``."
        ),
    )
    raw_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description="Pre-efficiency v5 quality score, when present.",
        ),
    ] = None
    efficiency_bonus: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=0.1,
            description=(
                "Frozen platform-side relative token-efficiency bonus fraction "
                "(bench_version >= 7 only). Assigned once against the frozen "
                "cohort snapshot of the epoch this submission finalized in and "
                "never recomputed. Strictly additive: never negative, capped at "
                "the epoch's frozen B_max. Null below bench_version 7, while "
                "the bonus is disabled/inactive, or before assignment."
            ),
        ),
    ] = None
    efficiency_factor: Annotated[
        float | None,
        Field(
            default=None,
            gt=0.0,
            description=(
                "Frozen efficiency factor. Curve v3 stays in [0.85, 1.10]; "
                "curve v4 is the unclamped power so cost can still order an "
                "exact-quality tier. Downside multiplies quality; upside "
                "scales remaining headroom (linear on v3, asymptotic on v4). "
                "When present it supersedes the legacy efficiency_bonus."
            ),
        ),
    ] = None
    efficiency_curve_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=3,
            description=(
                "Frozen efficiency curve that produced efficiency_factor. "
                "3 = bounded linear-headroom transform; 4 = unclamped "
                "asymptotic-headroom transform."
            ),
        ),
    ] = None
    efficiency_fold_applied: Annotated[
        bool,
        Field(
            description=(
                "Whether the surfaced efficiency adjustment is currently part "
                "of ranking, KOTH, and emissions. Curve-v3 does not modify "
                "official_composite; it breaks exact-quality ties. False "
                "means effective_composite is an audit-only projection."
            ),
        ),
    ] = False
    effective_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.1,
            description=(
                "Frozen-adjustment projection. Curve v3 multiplies downside or "
                "applies upside to pre_efficiency_composite's remaining "
                "headroom; legacy curves multiply by one plus their bonus. "
                "With curve-v3 active it is the secondary key only after exact "
                "official_composite equality and is computed independently of "
                "the model-use rollout gate; with the fold off it is audit-only. Null "
                "whenever both adjustment fields are null. Signed quality "
                "evidence is never modified."
            ),
        ),
    ] = None
    efficiency_snapshot_id: Annotated[
        UUID | None,
        Field(
            default=None,
            description=(
                "Id of the frozen cohort snapshot the bonus was computed "
                "against (the bonus_reference provenance pointer); resolvable "
                "at /public/efficiency/snapshots/{snapshot_id}. Null whenever "
                "efficiency_bonus is null."
            ),
        ),
    ] = None
    efficiency_bonus_preview: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=0.1,
            description=(
                "What the bonus WOULD be if the feature were switched on, "
                "computed at read time and persisted nowhere. Populated only "
                "while efficiency.preview is true, and deliberately kept "
                "separate from efficiency_bonus so that no consumer can mistake "
                "an unapplied preview for an awarded bonus. It is never folded "
                "into effective_composite and never reaches validator weights."
            ),
        ),
    ] = None
    efficiency_factor_preview: Annotated[
        float | None,
        Field(
            default=None,
            gt=0.0,
            description=(
                "What the current factor curve would assign if enabled. "
                "Computed at read time, persisted nowhere, never folded into "
                "ranking, and never sent to validators."
            ),
        ),
    ] = None
    composite_stderr: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            description=(
                "The exact standard error surfaced to the validator's KOTH fold: "
                "a stashed confirmation re-score SE when present, otherwise the "
                "between-validator SEM of the finalized k=3 quorum. This is the "
                "measurement uncertainty used by the public dethrone decision and "
                "the validator's indifference band. None when neither estimate is "
                "available."
            ),
        ),
    ]
    settled_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "The agent's finalized median on the settled (active) benchmark "
                "version. Only populated in authoritative mode while a rollout is "
                "collecting the next version; null when there is no open rollout "
                "or the agent never reached quorum on the active version. This is "
                "the comparable baseline the dashboard ranks by mid-rollout, even "
                "for agents whose headline composite already flipped to the "
                "desired version."
            ),
        ),
    ] = None
    rollout_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "Median of the agent's accepted scores on the desired (rolling "
                "out) benchmark version so far. Preliminary until "
                "rollout_score_count reaches score_quorum; null when there is no "
                "open rollout or no accepted score on the desired version yet."
            ),
        ),
    ] = None
    rollout_score_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Accepted validator scores on the desired benchmark version so "
                "far (the settlement state of rollout_composite, out of "
                "score_quorum). Null when there is no open rollout."
            ),
        ),
    ] = None
    calibration_brier: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "Mean Brier score over cases where the harness self-reported a "
                "confidence: mean((confidence - correct)^2), lower is better. "
                "Honest confidence minimizes it; always-100% does not. Advisory "
                "only; never folded into the composite, so a harness that omits "
                "confidence is unaffected. None when no case carried a confidence."
            ),
        ),
    ]
    calibration_n: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "How many cases carried a self-reported confidence (the sample "
                "behind calibration_brier). None when zero."
            ),
        ),
    ]
    tool_mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean tool accuracy in [0,1].")
    ]
    memory_mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean memory recall in [0,1].")
    ]
    first_seen: Annotated[
        datetime, Field(description="When the winning agent was first uploaded (UTC).")
    ]
    crown_first_seen: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "The arrival time the KOTH champion fold actually orders on "
                "(UTC), and the single most misread number on this board. It is "
                "the *lineage's* earliest arrival at a score within the "
                "dethrone margin of this entry, not this tarball's upload "
                "time, so a miner keeps its reign across a resubmission "
                "instead of forfeiting it by improving. When it is "
                "earlier than ``first_seen`` the difference comes from a sibling "
                "in ``submission_family`` -- match it against that member's "
                "``submitted_at`` to see which generation supplies it, and note "
                "the sibling may sit on a different ``miner_hotkey``, because "
                "owner families are resolved across attested payment roots. "
                "Null on rows built outside the owner-family read, where the "
                "fold falls back to ``first_seen``."
            ),
        ),
    ] = None
    median_ms: Annotated[
        int | None,
        Field(default=None, ge=0, description="Median per-case latency (ms)."),
    ]
    n: Annotated[
        int | None, Field(default=None, ge=0, description="Number of cases scored.")
    ]
    eligible: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "Whether this run administered the full benchmark and is therefore "
                "score-rank eligible. Current weight and emission eligibility also "
                "requires finalized=true and registered=true. False marks a "
                "provisional smoke/practice "
                "run (a smaller run-size profile that omits the hard memory "
                "categories): it is shown for transparency but is not ranked and "
                "never earns emissions. The rank field is only meaningful for "
                "eligible entries."
            ),
        ),
    ]
    bench_version: Annotated[
        int | None, Field(default=None, description="Benchmark scoring version.")
    ]
    dataset_sha256: Annotated[
        str | None,
        Field(default=None, description="SHA-256 of the scored dataset artifact."),
    ]
    models: Annotated[
        PublicRunModels | None,
        Field(default=None, description="LLM models that produced + graded the run."),
    ]
    per_category: Annotated[
        list[PublicCategoryStat] | None,
        Field(default=None, description="Per-category (per tool / memory type) means."),
    ]
    integrity: Annotated[
        PublicBenchIntegrity | None,
        Field(default=None, description="Anti-overfit / scoring-integrity telemetry."),
    ]
    tokens: Annotated[
        int | None,
        Field(default=None, ge=0, description="LLM tokens spent generating+judging."),
    ]
    token_usage: Annotated[
        PublicTokenUsage | None,
        Field(default=None, description="Validator-proxy token accounting."),
    ] = None
    model_use: Annotated[
        PublicModelUse | None,
        Field(
            default=None,
            description=(
                "Whether this run actually used the language model, with the "
                "measured ratios and the published thresholds behind the verdict."
            ),
        ),
    ] = None
    token_efficiency: Annotated[
        PublicTokenEfficiency | None,
        Field(default=None, description="Benchmark-v5 efficiency adjustment."),
    ] = None
    composite_breakdown: Annotated[
        PublicCompositeBreakdown | None,
        Field(
            default=None,
            description=(
                "Public-safe arithmetic showing the capability mean, combined "
                "pre-token benchmark gates, token adjustment, and final score."
            ),
        ),
    ] = None
    history: Annotated[
        list[float] | None,
        Field(
            default=None,
            description=(
                "This miner's recent composite scores, oldest→newest (across their "
                "submissions / re-scores), for a trend sparkline. Aggregate only: "
                "no seeds, no per-case content. None / omitted when there is no "
                "history beyond the current score."
            ),
        ),
    ]
    case_results: Annotated[
        list[PublicCaseResult] | None,
        Field(
            default=None,
            description=(
                "Redacted per-case results for detailed analysis: each case's "
                "category / kind / score / pass / latency / mechanical notes, but "
                "never the answer key (``expected`` / ``called`` / ``case_id``). "
                "None when the run carries no per-case data."
            ),
        ),
    ]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "rank": 1,
                "finalized": True,
                "score_count": 3,
                "score_quorum": 3,
                "miner_hotkey": "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
                "miner_uid": 42,
                "composite": 0.587,
                "composite_stderr": 0.014,
                "tool_mean": 0.867,
                "memory_mean": 0.167,
                "first_seen": "2026-07-03T20:00:00Z",
                "median_ms": 2720,
                "n": 12,
                "bench_version": 4,
                "dataset_sha256": "9f2c…",
                "models": {
                    "generator": "google/gemini-3.1-flash-lite",
                    "judge": "google/gemini-3.1-flash-lite",
                    "harness": "google/gemini-3.1-flash-lite",
                },
                "per_category": [
                    {"category": "memory_lookup", "count": 6, "mean": 1.0},
                    {"category": "web_search", "count": 1, "mean": 0.5},
                ],
                "integrity": {
                    "paraphrase_applied": 20,
                    "paraphrase_attempted": 20,
                    "paraphrase_fallback": 0,
                    "lexical_gap_rewritten": 2,
                    "lexical_gap_questions": 5,
                    "lexical_gap_mean_before": 0.45,
                    "lexical_gap_mean_after": 0.2,
                    "capped_tool_cases": 4,
                    "seeding_waves": 1,
                },
                "tokens": 7622,
                "history": [0.502, 0.548, 0.571, 0.587],
                "case_results": [
                    {
                        "category": "web_search",
                        "kind": "tool",
                        "score": 0.6,
                        "correct": False,
                        "latency_ms": 3382,
                        "notes": ["1 extra/unexpected tool call(s)"],
                    },
                    {
                        "category": "preference",
                        "kind": "memory",
                        "score": 1.0,
                        "correct": True,
                        "latency_ms": 1333,
                        "notes": ["deterministic answer match (no judge call)"],
                    },
                ],
            }
        }
    )


class PublicEmissionRecipient(BaseModel):
    """One miner projected to receive a non-zero share of the KOTH miner pool."""

    role: Annotated[
        Literal["champion", "joint_champion", "tail"],
        Field(description="Champion, score-ceiling joint champion, or tail recipient."),
    ]
    agent_id: Annotated[UUID, Field(description="The recipient's folded agent id.")]
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    raw_rank: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "This entry's independent rank by the finalized canonical "
                "median ``composite`` -- deliberately a different ordering from "
                "the board's ``rank``, which uses ``official_composite``. A "
                "champion carrying ``raw_rank: 4`` is not an inconsistency: it "
                "means three agents beat it on the single-quorum median while it "
                "leads on the continual mean that actually folds into emissions. "
                "Do not read this as a leaderboard position."
            ),
        ),
    ]
    share_of_miner_pool: Annotated[
        float,
        Field(
            gt=0.0,
            le=1.0,
            description=(
                "Relative KOTH weight within the miner pool, before the subnet's "
                "separate miner-emission cap."
            ),
        ),
    ]
    shared_seed_confirmations: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description=(
                "Shared-seed confirmation depth: distinct champion-anchored CRN "
                "seeds this recipient has been re-scored on by the continual "
                "rescore lane. Grows while the agent is in its configured cohort; "
                "a longer-reigning champion accumulates more. Zero until the lane "
                "has run, including for a joint-crown boundary tie outside the lane."
            ),
        ),
    ] = 0


class PublicDethroneDecision(BaseModel):
    """The raw leader's comparison against the incumbent KOTH champion."""

    model_config = ConfigDict(extra="ignore")

    challenger_lead: float
    required_lead: Annotated[float, Field(ge=0.0)]
    margin_lead: Annotated[float, Field(ge=0.0)]
    statistical_lead: Annotated[float | None, Field(default=None, ge=0.0)]
    method: Literal["flat", "unpaired", "paired"]
    dethrones: bool
    required_score: float
    score_ceiling: Annotated[float, Field(gt=0.0)]
    ceiling_deadlocked: bool
    paired_standard_error: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            description=(
                "Standard error of the per-seed challenger-minus-champion "
                "differences. Present only on a paired comparison. The "
                "statistical term is dethrone_z times this value, before "
                "high-score decay and the ceiling cap."
            ),
        ),
    ] = None
    shared_seed_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "How many shared confirmation seeds entered the paired "
                "comparison. Null when the fold used the unpaired or flat rule."
            ),
        ),
    ] = None
    seed_differences: Annotated[
        tuple[float, ...] | None,
        Field(
            default=None,
            description=(
                "Per-seed challenger minus champion composites, sorted by "
                "seed id. These are score differences, not case answers. "
                "Null when the fold is not a paired comparison."
            ),
        ),
    ] = None


class PublicKothEmissions(BaseModel):
    """Current read-only projection of the validator's KOTH weight fold."""

    margin: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description=(
                "Base composite-point lead before versioned high-score band scaling."
            ),
        ),
    ]
    dethrone_z: Annotated[float, Field(ge=0.0)]
    band_decay_min_bench_version: Annotated[
        int,
        Field(ge=1, description="First benchmark version using high-score decay."),
    ]
    band_decay_start_composite: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Incumbent composite above which the band begins shrinking.",
        ),
    ]
    band_decay_rate: Annotated[
        float,
        Field(gt=0.0, description="Exponential high-score band decay rate."),
    ]
    champion_share: Annotated[float, Field(gt=0.0, le=1.0)]
    rank_shares: tuple[Annotated[float, Field(gt=0.0, le=1.0)], ...]
    tie_weighting_active: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Whether the displayed recipient shares pool rank slots across "
                "exact or paired-evidence statistical ties."
            ),
        ),
    ] = False
    tie_weighting_required_protocol: Annotated[
        int,
        Field(
            default=20,
            ge=1,
            description="Minimum fleet heartbeat protocol for tie pooling.",
        ),
    ] = 20
    ceiling_headroom_share: Annotated[
        float,
        Field(
            default=0.5,
            gt=0.0,
            le=1.0,
            description=(
                "Largest share of the challenger's remaining headroom the "
                "dethrone band may consume once the ceiling-aware cap is "
                "active, so a perfect run always clears the crown."
            ),
        ),
    ] = 0.5
    ceiling_band_clamp_active: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Whether the displayed dethrone band is capped by the "
                "challenger's remaining headroom."
            ),
        ),
    ] = False
    ceiling_band_clamp_required_protocol: Annotated[
        int,
        Field(
            default=24,
            ge=1,
            description=(
                "Minimum fleet heartbeat protocol for the ceiling-aware band cap."
            ),
        ),
    ] = 24
    allocation_mode: Annotated[
        Literal["ranked", "score_ceiling_pool"],
        Field(
            default="ranked",
            description=(
                "Ranked champion-tail schedule, or an uncapped equal split by "
                "the best evidence-tied cohort when the crown cannot be beaten "
                "within the score domain."
            ),
        ),
    ] = "ranked"
    score_ceiling_pool_size: Annotated[int, Field(default=0, ge=0)] = 0
    tail_size: Annotated[int, Field(ge=0)]
    champion_agent_id: UUID
    champion_miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    raw_leader_agent_id: UUID
    raw_leader_miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    raw_leader_decision: PublicDethroneDecision | None = None
    champion_defense: Annotated[
        PublicDethroneDecision | None,
        Field(
            default=None,
            description=(
                "What the best rival miner would need to take the crown. Unlike "
                "``raw_leader_decision`` this is populated whenever any rival "
                "exists, including when the champion is itself the raw leader -- "
                "the case where the old field goes null and the board silently "
                "stopped answering the one question every challenger asks. Read "
                "``required_score`` against ``score_ceiling``: when "
                "``ceiling_deadlocked`` is true the requirement has passed above "
                "the highest score the domain can express, so no submission can "
                "dethrone this champion at all and only ``allocation_mode`` "
                "``score_ceiling_pool`` can redistribute the crown's share."
            ),
        ),
    ] = None
    recipients: list[PublicEmissionRecipient] = Field(default_factory=list)


class PublicEfficiencyStatus(BaseModel):
    """Where the relative token-efficiency bonus stands for a displayed board.

    Present for any bench_version >= 7 board. ``active=false`` means no bonus is
    being applied — either the frozen cohort has not reached the ``n_min``
    activation gate (after lineage dedupe), or the feature is switched off and
    this is a ``preview``.

    Read ``active`` and ``preview`` together:

    * ``active=true,  preview=false`` — live. Bonuses are frozen rows and
      ``effective_composite`` on each entry carries them.
    * ``active=false, preview=false`` — enabled but below the activation gate.
      Every bonus is zero and entries carry null efficiency fields.
    * ``active=false, preview=true``  — switched off. The numbers here are
      computed at read time from live state, persisted nowhere, applied to
      nothing, and never seen by the weight fold. Render them as "would be",
      never as an agent's score.
    """

    active: Annotated[
        bool,
        Field(description="Whether the governing frozen cohort awards bonuses."),
    ]
    preview: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True when this block was computed at read time and nothing "
                "was persisted or applied. Always accompanied by active=false."
            ),
        ),
    ]
    bench_version: Annotated[int, Field(ge=7)]
    run_size: Annotated[
        str, Field(description="Generator profile of the cohort (ranked = full).")
    ]
    epoch_index: Annotated[
        int,
        Field(description="Efficiency epoch ordinal of the governing snapshot."),
    ]
    snapshot_id: Annotated[
        UUID | None,
        Field(
            default=None,
            description=(
                "Frozen cohort snapshot id; resolvable at "
                "/public/efficiency/snapshots/{snapshot_id}. Null on a preview, "
                "which freezes nothing and therefore has nothing to resolve."
            ),
        ),
    ]
    cohort_size: Annotated[
        int,
        Field(ge=0, description="Deduped qualified cohort members at freeze time."),
    ]
    candidate_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Finalized ranked rows considered by a live preview; null for "
                "a frozen snapshot."
            ),
        ),
    ] = None
    cost_evidence_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Preview candidates with complete audited cost evidence; null "
                "for a frozen snapshot."
            ),
        ),
    ] = None
    quality_qualified_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Costed preview candidates clearing the quality floors; null "
                "for a frozen snapshot."
            ),
        ),
    ] = None
    owner_deduped_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Quality-qualified preview candidates after payment-owner "
                "dedupe; null for a frozen snapshot."
            ),
        ),
    ] = None
    lineage_deduped_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Quality-qualified preview candidates after owner and lineage "
                "dedupe; null for a frozen snapshot."
            ),
        ),
    ] = None
    n_min: Annotated[
        int, Field(ge=2, description="Activation gate on the deduped cohort size.")
    ]
    bonus_cap: Annotated[
        float,
        Field(
            gt=0.0,
            le=0.1,
            description="Frozen tier-1 bonus fraction B_max (the value at P25).",
        ),
    ]
    curve_version: Annotated[
        int,
        Field(
            default=1,
            ge=1,
            description=(
                "Frozen bonus-curve policy: 1 = single-tier (cap at/below "
                "P25), 2 = two-tier (cap ramps to deep_bonus_cap between P25 "
                "and the deep frontier, then saturates flat), 3 = bounded "
                "power factor around the P25 reference, 4 = unclamped power "
                "factor with asymptotic remaining-headroom upside (Bench v9+)."
            ),
        ),
    ] = 1
    deep_bonus_cap: Annotated[
        float | None,
        Field(
            default=None,
            gt=0.0,
            le=0.1,
            description=(
                "Frozen tier-2 saturation cap (two-tier curve only): the flat "
                "bonus at or below the deep frontier. Null under the "
                "single-tier policy."
            ),
        ),
    ] = None
    deep_frontier_tokens: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            description=(
                "The deep frontier in tokens (deep_frontier_ratio x P25): "
                "usage at or below it earns the flat deep_bonus_cap — no "
                "extra reward for racing further toward zero. Null while "
                "inactive or under the single-tier policy."
            ),
        ),
    ] = None
    factor_alpha: Annotated[
        float | None,
        Field(
            default=None,
            gt=0.0,
            le=1.0,
            description="Frozen power exponent for curve v3; null for v1/v2.",
        ),
    ] = None
    minimum_factor: Annotated[
        float | None,
        Field(
            default=None,
            gt=0.0,
            le=1.0,
            description="Frozen lower multiplier clamp. Applied by curve v3.",
        ),
    ] = None
    maximum_factor: Annotated[
        float | None,
        Field(
            default=None,
            ge=1.0,
            le=100.0,
            description="Frozen upper multiplier clamp. Applied by curve v3.",
        ),
    ] = None
    reference_p25_tokens: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            description=(
                "Nearest-rank P25 of the qualified cohort's audited chat-token "
                "costs. It is the efficient frontier for v1/v2 and the neutral "
                "Reference Cost (factor 1.0) for v3. Null while inactive."
            ),
        ),
    ] = None
    reference_median_tokens: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            description=(
                "Cohort median of audited chat token totals: usage at or above "
                "it earns zero bonus (linear in between). Null while inactive."
            ),
        ),
    ] = None


class PublicEfficiencyCohortMember(BaseModel):
    """One frozen cohort entry, public-safe (no raw lineage digests)."""

    agent_id: UUID
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    composite: Annotated[float, Field(ge=0.0, le=1.0)]
    memory_mean: Annotated[float, Field(ge=0.0, le=1.0)]
    token_total: Annotated[
        float,
        Field(
            ge=0.0,
            description=(
                "Audited chat-token cost: legacy quorum median or curve-v3 "
                "quorum-plus-comparable-retest arithmetic mean."
            ),
        ),
    ]
    lineage_group: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Opaque ordinal of this entry's lineage within the snapshot. "
                "The raw lineage digest (normalized-source / artifact hash) is "
                "moderation-adjacent and never exposed."
            ),
        ),
    ]
    collapsed_agent_ids: Annotated[
        list[UUID],
        Field(
            default_factory=list,
            description=(
                "Other qualified agents collapsed into this entry because they "
                "shared its lineage (one lineage cannot define the frontier)."
            ),
        ),
    ]


class PublicEfficiencySnapshotResponse(BaseModel):
    """One immutable frozen cohort snapshot — the full audit record a bonus is
    reproducible from (membership, floors, reference statistics)."""

    snapshot_id: UUID
    bench_version: Annotated[int, Field(ge=7)]
    run_size: str
    epoch_index: int
    active: bool
    cohort_limit: Annotated[int, Field(ge=2)]
    n_min: Annotated[int, Field(ge=2)]
    bonus_cap: Annotated[float, Field(gt=0.0, le=0.1)]
    curve_version: Annotated[int, Field(default=1, ge=1)] = 1
    deep_bonus_cap: Annotated[float | None, Field(default=None, gt=0.0, le=0.1)] = None
    deep_frontier_ratio: Annotated[
        float | None, Field(default=None, gt=0.0, lt=1.0)
    ] = None
    factor_alpha: Annotated[float | None, Field(default=None, gt=0.0, le=1.0)] = None
    minimum_factor: Annotated[float | None, Field(default=None, gt=0.0, le=1.0)] = None
    maximum_factor: Annotated[float | None, Field(default=None, ge=1.0, le=100.0)] = (
        None
    )
    quality_floor: Annotated[float, Field(ge=0.0, le=1.0)]
    memory_floor: Annotated[float, Field(ge=0.0, le=1.0)]
    reference_p25_tokens: Annotated[float | None, Field(default=None, ge=0.0)] = None
    reference_median_tokens: Annotated[float | None, Field(default=None, ge=0.0)] = None
    computed_at: datetime
    members: Annotated[
        list[PublicEfficiencyCohortMember],
        Field(default_factory=list, description="Frozen deduped cohort entries."),
    ]


class PublicLeaderboardResponse(BaseModel):
    """Raw score standings plus the current KOTH emissions projection."""

    generated_at: Annotated[
        datetime, Field(description="When this snapshot was read (UTC).")
    ]
    count: Annotated[int, Field(ge=0, description="Number of entries.")]
    current_bench_version: Annotated[
        int,
        Field(
            description=(
                "The latest DittoBench benchmark version. Entries whose "
                "bench_version is below this were scored on a previous benchmark "
                "and are not directly comparable; the UI marks them as such."
            )
        ),
    ]
    active_bench_version: Annotated[
        int,
        Field(description="Globally activated benchmark version."),
    ]
    desired_bench_version: Annotated[
        int,
        Field(
            description=(
                "Version currently being collected, or the active version when "
                "there is no open rollout."
            )
        ),
    ]
    available_bench_versions: Annotated[
        list[int],
        Field(
            default_factory=list,
            description=(
                "Every benchmark version with at least one accepted score, "
                "newest first. Drives the dashboard's per-version history "
                "pills; a new version appears here as soon as its first score "
                "lands."
            ),
        ),
    ]
    selection_mode: Annotated[
        Literal["authoritative", "historical"],
        Field(
            description=(
                "authoritative is the pool that drives validator weights: "
                "pinned to active_bench_version while a rollout is collecting "
                "(the desired version takes over only at rollout activation); "
                "historical is a requested single version."
            )
        ),
    ]
    v9_confirmation_mode: Annotated[
        Literal["shadow", "enforce"] | None,
        Field(
            default=None,
            description=(
                "Active Bench v9 confirmation policy. Shadow publishes measured "
                "LongMemEval and ablation evidence without changing ranking or "
                "emissions. Enforce makes full confirmation authoritative and "
                "suppresses base-only or provisional rows. Null means off."
            ),
        ),
    ] = None
    continual_aggregate_active: Annotated[
        bool,
        Field(
            description=(
                "Whether completed continual waves currently update rankings and "
                "validator weights. Activation is global and fail-closed until every "
                "recently-live validator supports the required protocol."
            )
        ),
    ] = False
    continual_aggregate_required_protocol: Annotated[int, Field(ge=1)] = 14
    registration_stale: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True when each entry's `registered` / `miner_uid` come from a "
                "previous successful chain read because the latest one failed. "
                "The values are still real, just not confirmed as of this "
                "response; a reader should label them rather than discard them. "
                "`registered` is null (genuinely unknown) instead when there is "
                "no recent good read to fall back on."
            ),
        ),
    ] = False
    entries: Annotated[
        list[PublicLeaderboardEntry],
        Field(default_factory=list, description="Ranked miners, best composite first."),
    ]
    emissions: Annotated[
        PublicKothEmissions | None,
        Field(
            default=None,
            description=(
                "Current KOTH fold over finalized, full-benchmark entries on the "
                "current benchmark. Null when no entry can receive emissions."
            ),
        ),
    ] = None
    efficiency: Annotated[
        PublicEfficiencyStatus | None,
        Field(
            default=None,
            description=(
                "Relative token-efficiency bonus status for this board. Null "
                "below bench_version 7, while the feature is disabled, or "
                "before the first cohort snapshot is frozen. active=false "
                "means the frozen cohort has not reached its n_min activation "
                "gate and every bonus is zero."
            ),
        ),
    ] = None


class PublicChainWeight(BaseModel):
    """One non-zero destination in a validator's revealed chain vector."""

    uid: Annotated[int, Field(ge=0)]
    hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    value: Annotated[int, Field(gt=0, le=65535)]


class PublicValidatorWeightVector(BaseModel):
    """One validator's latest publicly revealed on-chain weights."""

    validator_uid: Annotated[int, Field(ge=0)]
    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    weights: list[PublicChainWeight] = Field(default_factory=list)


class PublicChainEpoch(BaseModel):
    """When SN118 next folds weights into emissions, and how often it does.

    Answers the question the weight matrix cannot: validators commit weights
    *asynchronously* — each one only has to respect ``weights_rate_limit_blocks``
    between its own submissions — so there is no single moment at which "the
    validators set weights". What is synchronised is the subnet's epoch tick:
    every ``tempo_blocks`` blocks Subtensor runs Yuma consensus over whatever
    weights are revealed at that moment and pays out the emission accumulated
    since the last tick. That tick is the same instant for every miner on the
    subnet, and it is what a miner asking "when do I get emissions" means.

    Under commit-reveal a commit is not folded at the next tick but at the one
    ``reveal_period_epochs`` later, which is why a validator's newest opinion
    can be invisible in the matrix beside this.
    """

    tempo_blocks: Annotated[
        int,
        Field(
            gt=0,
            description=(
                "Blocks between epoch ticks (`SubtensorModule.Tempo`). SN118 "
                "runs 360, about 72 minutes."
            ),
        ),
    ]
    block_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            description=(
                "Nominal seconds per block used to turn the block counts here "
                "into times. Subtensor targets 12s; real blocks vary slightly, "
                "so every time on this object is an estimate, not a promise."
            ),
        ),
    ]
    epoch_seconds: Annotated[
        float,
        Field(
            gt=0.0,
            description=(
                "Nominal seconds per epoch (`tempo_blocks` x `block_seconds`). "
                "A client whose `next_epoch_at` has already passed — because it "
                "is holding a cached snapshot — can roll it forward by this."
            ),
        ),
    ]
    last_epoch_block: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Block at which the subnet's last epoch tick ran, read from "
                "chain rather than computed, so the phase cannot drift from "
                "what Subtensor actually did."
            ),
        ),
    ]
    next_epoch_block: Annotated[
        int,
        Field(ge=0, description="Block at which the next tick is due."),
    ]
    blocks_since_last_epoch: Annotated[
        int, Field(ge=0, description="Blocks elapsed at the snapshot's block.")
    ]
    blocks_until_next_epoch: Annotated[
        int, Field(ge=0, description="Blocks remaining at the snapshot's block.")
    ]
    next_epoch_at: Annotated[
        datetime,
        Field(
            description=(
                "Estimated UTC time of the next tick, anchored on the snapshot "
                "block's own on-chain timestamp (not the API server's clock). "
                "Absolute rather than a duration so a cached response does not "
                "hand out a countdown that is already spent."
            )
        ),
    ]
    commit_reveal_enabled: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Whether weight commitments are timelock-encrypted. When true "
                "the published matrix necessarily lags active commitments."
            ),
        ),
    ] = None
    reveal_period_epochs: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Epochs between a commit and its reveal. With commit-reveal on "
                "and this at 1, weights committed during one epoch are folded "
                "at the end of the next one."
            ),
        ),
    ] = None
    weights_rate_limit_blocks: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Chain-enforced minimum blocks between one hotkey's weight "
                "submissions. This is the only cadence a validator is held to; "
                "it is why submissions are staggered rather than simultaneous."
            ),
        ),
    ] = None


class PublicChainWeightsResponse(BaseModel):
    """Block-consistent SN118 weight matrix read from Subtensor storage."""

    generated_at: Annotated[
        datetime,
        Field(
            description=(
                "When this matrix was read from chain (UTC) — not when the "
                "response was served. The read is cached, so a response can be "
                "served some time after `generated_at`; `age_seconds` is the gap."
            )
        ),
    ]
    netuid: Annotated[int, Field(ge=0)]
    block: Annotated[int, Field(ge=0)]
    block_hash: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    owner_hotkey: Annotated[str | None, Field(default=None, pattern=_SS58_PATTERN)]
    vectors: list[PublicValidatorWeightVector] = Field(default_factory=list)
    stale: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True when the most recent attempt to re-read the matrix failed "
                "and this is the last known good one. The block it pins is real "
                "chain state, just older than a normal response; a reader should "
                "label it rather than treat the matrix as absent."
            ),
        ),
    ] = False
    age_seconds: Annotated[
        float,
        Field(
            default=0.0,
            ge=0.0,
            description="Seconds between the chain read and this response.",
        ),
    ] = 0.0
    epoch: Annotated[
        PublicChainEpoch | None,
        Field(
            default=None,
            description=(
                "Where the subnet sits in its tempo cycle at `block` — the "
                "countdown to the next weight fold and emission payout. Null "
                "when the hyperparameter reads failed; the matrix is this "
                "endpoint's contract and the epoch is decoration on it, so a "
                "failed epoch read degrades the countdown rather than the "
                "response."
            ),
        ),
    ] = None


class PublicValidatorScore(BaseModel):
    """One validator's score for a submission, published verbatim (public).

    The per-validator half of the k=3 transparency record: *which* validator
    scored the agent and the exact numbers it reported, including its sr25519
    ``signature`` so the row is independently verifiable against the published
    validator public key. Unlike the aggregate leaderboard this deliberately
    exposes ``validator_hotkey`` (a public on-chain identity) and the raw
    ``seed``: the whole point of the record is to show *who* scored an agent on
    *which* dataset, so an observer can reproduce and audit the number.
    """

    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Scoring validator's hotkey.")
    ]
    composite: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Composite this validator reported in [0,1].",
        ),
    ]
    tool_mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean tool accuracy in [0,1].")
    ]
    memory_mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean memory recall in [0,1].")
    ]
    raw_composite: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Pre-token composite."),
    ] = None
    token_usage: PublicTokenUsage | None = None
    model_use: PublicModelUse | None = None
    token_efficiency: PublicTokenEfficiency | None = None
    composite_breakdown: PublicCompositeBreakdown | None = None
    v9_base: PublicV9BaseEvidence | None = None
    median_ms: Annotated[int, Field(ge=0, description="Median per-case latency (ms).")]
    n: Annotated[int, Field(ge=0, description="Number of cases scored.")]
    bench_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "DittoBench version this validator's run was scored under. A "
                "re-scored agent carries rows from more than one version, and "
                "composites compare only within a version. Null for a legacy "
                "score recorded before benchmark versioning."
            ),
        ),
    ]
    seed: Annotated[
        int,
        Field(
            description=(
                "Dataset seed this validator scored on. The platform draws it "
                "after screening (the miner never sees it before submitting), so "
                "publishing it post-hoc enables reproduction/audit without letting "
                "anyone pre-overfit a future submission."
            )
        ),
    ]
    run_id: Annotated[
        str, Field(description="Scoring-engine run id the signature is bound to.")
    ]
    ticket_deadline: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "Exact ticket lease bound into current score signatures. Null "
                "identifies a legacy score recorded before lease-bound signing."
            ),
        ),
    ]
    signature: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "sr25519 signature over the score payload, hex. Current signatures "
                "include ticket_deadline; legacy rows with a null deadline use the "
                "pre-lease payload and remain valid."
            ),
        ),
    ]
    generated_at: Annotated[
        datetime, Field(description="When the scoring engine produced the score (UTC).")
    ]
    transform_robustness: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "Reproduce-under-transform audit result: the fraction of audit "
                "pairs this run answered consistently. A share of every run's "
                "cases is re-asked under a rephrasing (or a shift that moves the "
                "answer) derived from the block-hash-seeded dataset seed, which "
                "postdates the submission's commit -- so the miner could not have "
                "pre-handled it. What a low value measures is SURFACE "
                "BRITTLENESS (right on the phrasing the harness was built for, "
                "wrong on one it was not) or MEMORIZATION; it is not evidence "
                "about a harness that genuinely recomputes the answer, which "
                "scores the same under the transform. Both the selection and the "
                "transforms are pure functions of the published seed, so anyone "
                "can regenerate the audit set and recheck this number. Null for "
                "a run that carried no audit pairs or predates the audit."
            ),
        ),
    ]
    audit_case_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "How many audit pairs backed ``transform_robustness``, so a value "
                "backed by many pairs is distinguishable from one backed by two."
            ),
        ),
    ]
    case_results: Annotated[
        list[PublicCaseResult] | None,
        Field(
            default=None,
            description=(
                "Redacted per-case breakdown of this validator's run: each case's "
                "category / kind / score / pass / latency / mechanical notes, so an "
                "observer can audit exactly where the agent gained or lost points. "
                "Never the answer key (expected / called / case_id). None when the "
                "run carries no per-case data."
            ),
        ),
    ]
    transcript_sha256: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "SHA-256 of this validator's published transcript artifact (the "
                "graded per-case inputs), bound into the score signature. The "
                "bytes live content-addressed in the public bucket at "
                "``transcripts/{sha256}.json``; regenerating the dataset from "
                "the seed and re-running the public grader over the transcript "
                "reproduces this score offline. Null for scores whose validator "
                "published no transcript."
            ),
        ),
    ]


class PublicSubmissionScores(BaseModel):
    """The full k=3 scoring record for one submission (public transparency).

    Publishes, per agent: which validators scored it, each validator's exact
    numbers + signature, and the ``median_composite`` the platform finalized on
    (the canonical score no single validator controls). ``score_count`` reaching
    ``quorum`` is what finalized the agent; a re-scored agent may carry more than
    ``quorum`` rows (older + current runs). The dataset pin (``dataset_seed`` +
    ``dataset_sha256``) identifies the exact bytes all validators scored.
    """

    agent_id: Annotated[UUID, Field(description="The scored agent's id.")]
    miner_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Submitting miner's SS58 hotkey.")
    ]
    status: Annotated[str, Field(description='Public status ("scored" or "live").')]
    artifact_release: PublicArtifactRelease
    quorum: Annotated[
        int, Field(ge=1, description="Validators required to finalize (k=3).")
    ]
    score_count: Annotated[
        int, Field(ge=0, description="Score rows recorded for this agent.")
    ]
    median_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description="Median canonical composite in [0,1].",
        ),
    ]
    v9_confirmation_status: Annotated[
        Literal["base_only", "provisional", "full_confirmed"] | None,
        Field(default=None, exclude_if=lambda value: value is None),
    ] = None
    v9_full_confirmed_composite: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, exclude_if=lambda value: value is None),
    ] = None
    v9_shadow_quality_composite: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, exclude_if=lambda value: value is None),
    ] = None
    v9_longmem_mean_composite: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, exclude_if=lambda value: value is None),
    ] = None
    v9_confirmation_receipt: Annotated[
        V9ConfirmationReceipt | None,
        Field(
            default=None,
            exclude_if=lambda value: value is None,
            description=(
                "Signed evidence root and subject projection used to reproduce "
                "the reward-authoritative full v9 composite."
            ),
        ),
    ] = None
    dataset_seed: Annotated[
        int | None,
        Field(default=None, description="Platform-pinned dataset seed (regenerable)."),
    ]
    dataset_sha256: Annotated[
        str | None,
        Field(default=None, description="SHA-256 of the pinned dataset artifact."),
    ]
    dataset_run_size: Annotated[
        str | None,
        Field(default=None, description="Generator profile (small|medium|full)."),
    ]
    dataset_seed_block: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "On-chain block number the seed was derived from. Fetch this "
                "block's hash and recompute derive_seed(hash, agent_id) to verify "
                "the seed was not platform-chosen. Null on the CSPRNG fallback "
                "(chain was unavailable at job-ready)."
            ),
        ),
    ]
    dataset_seed_block_hash: Annotated[
        str | None,
        Field(
            default=None,
            description="Hash of dataset_seed_block; the seed's verification input.",
        ),
    ]
    scores: Annotated[
        list[PublicValidatorScore],
        Field(default_factory=list, description="Per-validator scores, by hotkey."),
    ]
    generated_at: Annotated[
        datetime, Field(description="When this snapshot was read (UTC).")
    ]


class PublicSubmissionSummary(BaseModel):
    """One row of the public recent-submissions index (drill into the detail)."""

    agent_id: Annotated[UUID, Field(description="The scored agent's id.")]
    miner_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Submitting miner's SS58 hotkey.")
    ]
    status: Annotated[str, Field(description='Public status ("scored" or "live").')]
    artifact_release: PublicArtifactRelease
    score_count: Annotated[
        int, Field(ge=0, description="Score rows recorded for this agent.")
    ]
    median_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description="Median canonical composite in [0,1].",
        ),
    ]
    dataset_seed: Annotated[
        int | None, Field(default=None, description="Platform-pinned dataset seed.")
    ]
    dataset_sha256: Annotated[
        str | None, Field(default=None, description="SHA-256 of the pinned dataset.")
    ]
    last_scored_at: Annotated[
        datetime | None,
        Field(default=None, description="Most recent score time for this agent (UTC)."),
    ]


class PublicSubmissionsResponse(BaseModel):
    """The public recent-submissions index, most recently scored first."""

    generated_at: Annotated[
        datetime, Field(description="When this snapshot was read (UTC).")
    ]
    count: Annotated[int, Field(ge=0, description="Number of submissions returned.")]
    quorum: Annotated[
        int, Field(ge=1, description="Validators required to finalize (k=3).")
    ]
    submissions: Annotated[
        list[PublicSubmissionSummary],
        Field(default_factory=list, description="Recent finalized submissions."),
    ]


class PublicBenchmarkProgress(BaseModel):
    """Ticket-validated public benchmark progress allowlist.

    The allowlist itself is unchanged and remains closed: stage plus aggregate
    counts, never per-case identity, question text, verdicts, seeds or timings.
    ``percent`` is no longer quantized to 5% buckets — it is derived from the
    exact ``completed_checks``/``total_checks`` already on this model, which an
    observer can divide anyway, so the quantizer degraded the progress bar
    without withholding anything.
    """

    agent_id: UUID
    slot_id: str = "slot-0"
    agent_name: str
    bench_version: Annotated[
        int, Field(ge=1, description="DittoBench contract bound to this ticket.")
    ]
    started_at: Annotated[
        datetime, Field(description="When the validator ticket was issued (UTC).")
    ]
    stage: BenchmarkProgressStage | None = None
    completed_checks: Annotated[int | None, Field(default=None, ge=0)] = None
    total_checks: Annotated[int | None, Field(default=None, ge=1)] = None
    percent: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            le=100,
            description=(
                "Exact completion percentage from the reported check counts. "
                "Held below 100 until the run reaches finalizing/submitting, so "
                "a full bar always means the work is actually finished."
            ),
        ),
    ] = None
    stalled: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "The run has taken far longer than its own reported progress "
                "allows — either sitting in an early stage (preparing/building/"
                "generating/starting the harness) past the point that stage "
                "should ever take, or running with a check count too frozen to "
                "explain the elapsed time. Distinct from validator liveness: a "
                "stalled run still heartbeats, while a validator that has "
                "stopped reporting is surfaced as offline/heartbeat_stale."
            ),
        ),
    ] = False
    purpose: TicketPurpose = TicketPurpose.LEGACY_UNCLASSIFIED
    """Why the occupying lease was issued. Continual-retest tickets on already
    scored agents look identical to canonical work until this field is set.
    """


class PublicConfirmationSubject(BaseModel):
    """Public-safe subject identity in one shared confirmation bundle."""

    agent_id: UUID
    agent_name: str


class PublicConfirmationProgress(BaseModel):
    """Live LongMemEval/ablation work on independent validator capacity."""

    bundle_id: UUID
    slot_id: str
    bench_version: V9EvidenceBenchVersion = 9
    mode: Literal["shadow", "enforce"]
    profile_revision: str
    attempt: Annotated[int, Field(ge=1)]
    issued_at: datetime
    deadline: datetime
    stage: ConfirmationProgressStage | None = None
    completed: Annotated[int | None, Field(default=None, ge=0)] = None
    total: Annotated[int | None, Field(default=None, ge=1)] = None
    reported_agent_id: UUID | None = None
    progress_reported_at: datetime | None = None
    subjects: list[PublicConfirmationSubject] = Field(default_factory=list)


class PublicActivityEntry(BaseModel):
    """One submission's safe, public lifecycle state."""

    agent_id: Annotated[UUID, Field(description="The submitted agent's id.")]
    miner_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Submitting miner's SS58 hotkey.")
    ]
    name: Annotated[str, Field(description="Miner-provided agent display name.")]
    name_handle: PublicNameHandle | None = Field(
        default=None,
        description=(
            "Signed handle reservation touching this name, when one exists. "
            "Same semantics as the public leaderboard annotation."
        ),
    )
    avatar_url: str | None = Field(
        default=None,
        description="Public URL for this miner's signed profile picture, if set.",
    )
    version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Submission version within this named agent; null for legacy uploads."
            ),
        ),
    ] = None
    status: Annotated[
        str,
        Field(
            description=(
                "Public lifecycle stage. Internal review and enforcement states are "
                "collapsed to under_review or rejected."
            )
        ),
    ]
    artifact_release: PublicArtifactRelease
    submitted_at: Annotated[
        datetime, Field(description="When the platform accepted the upload (UTC).")
    ]
    last_scored_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description="When the platform most recently recorded a score (UTC).",
        ),
    ]
    screening_reason: Annotated[
        str | None,
        Field(default=None, description="Public-safe screening failure category."),
    ]
    duplicate_of: Annotated[
        UUID | None,
        Field(default=None, description="Earlier agent this submission may duplicate."),
    ]
    duplicate_name: Annotated[
        str | None,
        Field(default=None, description="Name of the matched submission."),
    ]
    duplicate_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description="Version of the matched submission; null when it is legacy.",
        ),
    ]
    duplicate_hotkey: Annotated[
        str | None,
        Field(
            default=None,
            pattern=_SS58_PATTERN,
            description=(
                "Hotkey of the matched submission. Equal to miner_hotkey when "
                "this hold is a same-miner rename or re-upload of that earlier "
                "row, not a comparison against someone else's agent."
            ),
        ),
    ]
    review_reason: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Public operator reason for the current ATH lifecycle event. For an "
                "active reopened hold this is the reopen reason, not the historical "
                "reason that first routed the submission to review. For a resolved "
                "review this is the resolution reason."
            ),
        ),
    ]
    review_event: Annotated[
        Literal["opened", "reopened", "cleared", "rejected"] | None,
        Field(
            default=None,
            description=(
                "Latest public ATH lifecycle event. Null when the submission has no "
                "durable ATH review record."
            ),
        ),
    ] = None
    review_event_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description="When the latest public ATH lifecycle event occurred (UTC).",
        ),
    ] = None
    review_original_reason: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Reason for the original ATH hold, retained as historical context. "
                "Clients must not present it as the current reason after a reopen or "
                "resolution."
            ),
        ),
    ] = None
    review_opened_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description="When the active ATH review hold began (UTC).",
        ),
    ]
    preserved_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=("Median composite preserved while an ATH review is active."),
        ),
    ]
    score_count: Annotated[
        int,
        Field(ge=0, description="Independent validator scores recorded so far."),
    ]
    provisional_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description="Mean composite across accepted validator scores so far.",
        ),
    ]
    validator_queue_rank: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Current global validator-assignment priority for a waiting "
                "submission, produced by the same ordering the allocator issues "
                "tickets with. It is an ordering, not a prediction of the next "
                "assignment: each validator additionally applies its own retry "
                "cooldowns, one-score-per-validator rule, and artifact mode, "
                "any of which can skip a row. Read validator_queue_gate before "
                "reading rank 1 as imminent."
            ),
        ),
    ]
    validator_queue_gate: Annotated[
        Literal[
            "previous_generation",
            "owner_serialized",
            "similarity_serialized",
            "not_leasable",
        ]
        | None,
        Field(
            default=None,
            description=(
                "Why this submission cannot be leased on the next poll despite "
                "its rank, or null when nothing holds it. 'previous_generation' "
                "is retired-era work the fleet serves only once the current era "
                "drains; 'owner_serialized' means another submission from the "
                "same paid owner is using the owner's validator slot, so this "
                "one waits while any other owner has eligible work -- rotating "
                "hotkeys does not buy a second slot, though a validator that "
                "finds nothing else eligible anywhere may still lease it rather "
                "than idle, up to the operator's per-owner limit; "
                "'similarity_serialized' means a near-identical submission is "
                "already using this one's share of fleet capacity, whichever "
                "key paid for it -- a queue-fairness wait and nothing more, "
                "carrying no claim that either submission is illegitimate, and "
                "it clears on its own when the other lease ends; "
                "'not_leasable' means the allocator's candidate filter excludes "
                "it (no versioned dataset, no eligible screened image, "
                "withdrawn, not admitted to this era, or every quorum slot "
                "already occupied)."
            ),
        ),
    ]
    validator_queue_gate_detail: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Human-readable evidence behind validator_queue_gate, or null "
                "when the reason code says everything. Populated for "
                "'similarity_serialized', which is the one gate a miner cannot "
                "reconstruct from their own submission: it names the "
                "near-identical submissions currently holding the shared "
                "concurrency budget and the measured overlap that grouped them, "
                "so the wait is checkable rather than mysterious. It is a "
                "capacity statement and carries no claim about the legitimacy "
                "of any submission named in it."
            ),
        ),
    ] = None
    previous_generation: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Whether this waiting submission predates the active benchmark "
                "era and is therefore reachable only by the previous-generation "
                "lanes (carryover and retired-era source backfill). Those lanes "
                "are strictly last and issue only into an empty current-era "
                "queue, so such a row is not advancing while any current-era "
                "submission still needs a validator -- however low its rank "
                "number happens to look."
            ),
        ),
    ] = False
    quorum: Annotated[
        int,
        Field(ge=1, description="Independent validator scores required to finalize."),
    ]
    retry_state: Annotated[
        RetryState | None,
        Field(
            default=None,
            description=(
                "Why a below-quorum submission is or isn't advancing: running, "
                "retry_available, cooling_down, exhausted (needs operator "
                "recovery), or queued. Null once finalized or not yet evaluating."
            ),
        ),
    ] = None
    retry_after: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "Earliest time an expired ticket becomes eligible to retry (UTC); "
                "set while cooling_down."
            ),
        ),
    ] = None
    screening_policy_version: Annotated[
        int, Field(ge=0, description="Latest completed screening policy version.")
    ]
    required_screening_policy_version: Annotated[
        int, Field(ge=1, description="Policy currently required by the platform.")
    ]
    screening_attempt_id: Annotated[
        UUID | None, Field(default=None, description="Active screening lease, if any.")
    ]
    screening_build_only: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Whether the active screener lease is mechanical image admission "
                "only. False means the source-review path; null means no active "
                "screener lease."
            ),
        ),
    ] = None
    screening_started_at: Annotated[
        datetime | None, Field(default=None, description="Active attempt start time.")
    ]
    screening_deadline: Annotated[
        datetime | None, Field(default=None, description="Active attempt deadline.")
    ]
    active_benchmarks: list[PublicBenchmarkProgress] = Field(default_factory=list)


class PublicActivityResponse(BaseModel):
    """Recent submission activity, newest first."""

    generated_at: Annotated[
        datetime, Field(description="When this snapshot was read (UTC).")
    ]
    count: Annotated[int, Field(ge=0, description="Number of submissions returned.")]
    total: Annotated[int, Field(ge=0, description="Total number of submissions.")]
    status_counts: Annotated[
        dict[str, int],
        Field(
            default_factory=dict,
            description=(
                "Counts by canonical public lifecycle stage before status filters "
                "are applied. Search filtering is reflected when present."
            ),
        ),
    ]
    downloadable_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Number of submissions whose source is currently available for "
                "public download, before status filters are applied. Search filtering "
                "is reflected when present."
            ),
        ),
    ] = 0
    page: Annotated[int, Field(ge=1, description="Current one-based page number.")]
    page_size: Annotated[int, Field(ge=1, description="Maximum entries per page.")]
    total_pages: Annotated[
        int, Field(ge=1, description="Total pages, or one when there are no entries.")
    ]
    entries: Annotated[
        list[PublicActivityEntry],
        Field(default_factory=list, description="Recent submissions, newest first."),
    ]


class PublicScreenerWatchdogResponse(BaseModel):
    """Minimal signal used by the independent GCE scale-out watchdog."""

    generated_at: datetime
    controller_stale: bool
    activate_fallback: bool
    reason: Literal[
        "controller_fresh",
        "controller_stale",
        "controller_missing",
        "provider_not_ready",
    ]
    controller_epoch: str | None = None
    controller_source_sha: str | None = None
    provider_ready: bool = False


class PublicScreeningReviewEvidence(BaseModel):
    """One public-safe policy observation from a terminal cheating decision."""

    module: Annotated[str, Field(min_length=1, max_length=64)]
    code: Annotated[str, Field(min_length=1, max_length=64)]
    summary: Annotated[str, Field(min_length=1, max_length=240)]


class PublicScreeningReviewLocation(BaseModel):
    """One source location published only after a terminal rejection."""

    path: Annotated[str, Field(min_length=1, max_length=240)]
    line: Annotated[int, Field(ge=1)]
    category: Annotated[str, Field(min_length=1, max_length=64)]


class PublicScreeningReviewFinding(BaseModel):
    """Digest-verified final finding safe for public rejected-attempt feedback."""

    reviewer_revision: Annotated[str, Field(min_length=1, max_length=64)]
    risk_level: Literal["low", "medium", "high"]
    confidence: Annotated[float, Field(ge=0, le=1)]
    categories: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]],
        Field(min_length=1, max_length=8),
    ]
    locations: Annotated[
        list[PublicScreeningReviewLocation], Field(default_factory=list, max_length=16)
    ]
    summary: Annotated[str, Field(min_length=1, max_length=240)]


class PublicScreeningAttempt(BaseModel):
    """One append-only screening attempt shown in submission details."""

    attempt_id: UUID
    policy_version: Annotated[int, Field(ge=1)]
    status: Annotated[
        str,
        Field(pattern=r"^(running|passed|rejected|failed|expired|quarantined)$"),
    ]
    screener_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    started_at: datetime
    deadline: datetime
    finished_at: datetime | None = None
    reason: str | None = None
    quarantine_resolution: Literal["release", "rescreen", "reject"] | None = None
    quarantine_resolved_at: datetime | None = None
    quarantine_resolution_reason: str | None = None
    review_evidence: list[PublicScreeningReviewEvidence] = Field(default_factory=list)
    review_finding: PublicScreeningReviewFinding | None = None


class PublicAdmissionRetry(BaseModel):
    """Live admission state for a submission still in build & admission.

    Failed cost-bearing attempts never retry automatically. ``parked`` names a
    source-review/provider failure (including OpenRouter throttling), while
    ``stuck`` names another Ditto-owned infrastructure failure, including a
    dropped screener lease (``worker-lease-orphaned``). Both require a
    guarded Backroom retry. ``retry_queued`` means that exact retry has already
    been authorized and is waiting for a screener slot.
    """

    state: Literal["queued", "running", "parked", "stuck", "retry_queued"]
    attempt_count: Annotated[int, Field(ge=0)]
    # Kept nullable for rolling compatibility with the pre-fail-once contract.
    # Manual retries do not have a scheduled retry time.
    next_retry_at: datetime | None = None
    last_failure_infrastructure: bool = False


class PublicScreeningDispute(BaseModel):
    """Public-safe appeal state; the miner's private message is never exposed."""

    status: Literal["pending", "resolved"]
    submitted_at: datetime
    resolved_at: datetime | None = None
    resolution: Literal["release", "uphold"] | None = None


class CreateScreeningDisputeRequest(BaseModel):
    """One signed appeal of a rejected screening decision."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    message: Annotated[str, Field(min_length=20, max_length=1000)]
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]


class CreateScreeningDisputeResponse(BaseModel):
    dispute: PublicScreeningDispute


PublicValidationFailureCode = Literal[
    "inference_allowance_exhausted",
    "inference_request_rejected",
    "model_inference_required",
    "inference_lane_saturated",
    "provider_recovery_exhausted",
    "grant_decline_evidence_mismatch",
    "budget_evidence_absent",
]

_PUBLIC_AGENT_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "inference_allowance_exhausted",
        "inference_request_rejected",
        "model_inference_required",
    }
)
_PUBLIC_INFRA_RELAY_CAUSES: frozenset[str] = frozenset(
    {
        "inference_lane_saturated",
        "provider_recovery_exhausted",
        "grant_decline_evidence_mismatch",
        "budget_evidence_absent",
    }
)


def public_validation_failure_code(
    failure_detail: str | None,
) -> PublicValidationFailureCode | None:
    """Map a ticket ``failure_detail`` onto the public allowlist, or None.

    Agent-attributable codes are stored as the exact detail string.
    Infrastructure relay causes are stored as ``{code}:{cause}`` so old
    validators still treat them as no-fault ``model_relay_unavailable``; the
    public field publishes the cause alone.
    """
    if failure_detail is None:
        return None
    if failure_detail in _PUBLIC_AGENT_FAILURE_CODES:
        return cast(PublicValidationFailureCode, failure_detail)
    if failure_detail in _PUBLIC_INFRA_RELAY_CAUSES:
        return cast(PublicValidationFailureCode, failure_detail)
    if ":" in failure_detail:
        _code, cause = failure_detail.split(":", 1)
        if cause in _PUBLIC_INFRA_RELAY_CAUSES:
            return cast(PublicValidationFailureCode, cause)
    return None


class PublicValidationAttempt(BaseModel):
    """One validator ticket for either quorum scoring or continual retesting.

    A ticket row is a *lease slot*, not an append-only attempt log: the composite
    PK keeps one row per (agent, bench version, validator) and every reissue
    rewrites it in place, bumping :attr:`attempt_count` and resetting
    :attr:`issued_at`. ``failure_reason``/``failed_at`` are deliberately
    preserved across that reissue, so they can describe a *previous* lease that
    has since been retried and even scored. Consumers must read them relative to
    ``issued_at`` — a ``failed_at`` older than ``issued_at`` belongs to an
    earlier lease and is history, not the current state of this slot.
    """

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    status: Annotated[str, Field(pattern=r"^(issued|scored|expired)$")]
    purpose: TicketPurpose = TicketPurpose.LEGACY_UNCLASSIFIED
    issued_at: datetime
    deadline: datetime
    bench_version: Annotated[int, Field(ge=1)]
    actively_running: bool = False
    benchmark_progress: PublicBenchmarkProgress | None = None
    failure_reason: Literal["infrastructure", "scoring_error", "sandbox_oom"] | None = (
        None
    )
    failure_code: PublicValidationFailureCode | None = None
    """Allowlisted machine cause behind ``failure_reason``.

    The validator's free-form diagnostic remains private. This field publishes
    only machine codes whose meaning is safe and useful to a miner; it is never
    derived from arbitrary message text. Agent-attributable codes are terminal.
    Infrastructure relay causes stay no-fault (the dashboard still marks them
    deferred) so miners can see *why* a run was not scored.
    """
    failed_at: datetime | None = None
    attempt_count: Annotated[int, Field(ge=1)] = 1
    """Leases issued to this validator for this agent/bench version. Greater
    than one means earlier attempts preceded the one described here."""


class PublicInferenceRun(BaseModel):
    """Platform-metered inference spend for one validator benchmark lease.

    One inference grant is minted per lease, so this is the durable run-level
    accounting ledger. It exposes aggregate counts and cost only: no provider
    route, prompts, responses, request bodies, or per-case content.
    """

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=1)]
    ticket_deadline: datetime
    status: Literal["pending", "active", "revoked", "exhausted"]
    request_budget: Annotated[int, Field(ge=1)]
    requests: Annotated[int, Field(ge=0)]
    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]
    token_budget: Annotated[int, Field(ge=1)]
    embedding_requests: Annotated[int, Field(ge=0)]
    embedding_tokens: Annotated[int, Field(ge=0)]
    cost_microusd: Annotated[int, Field(ge=0)]
    accounting_version: Annotated[int, Field(ge=1)]
    created_at: datetime
    updated_at: datetime


class PublicConfirmationScore(BaseModel):
    """One append-only shared-seed score from a continual top-five retest."""

    composite: Annotated[float, Field(ge=0.0, le=1.0)]
    seed: Annotated[
        str,
        Field(
            pattern=r"^\d+$",
            description="Exact decimal shared seed, encoded without JS rounding.",
        ),
    ]
    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=1)]
    accepted_at: datetime


class PublicProvisionalScore(BaseModel):
    """One score the platform accepted toward a submission's quorum.

    This deliberately exposes only the numeric composite, the deterministic
    dataset inputs needed to reproduce it, and the same redacted per-question
    outcomes shown for finalized scores. Validator identity, signatures, ticket
    leases, answer keys, and scorer internals remain outside the public
    in-progress surface.
    """

    composite: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Accepted composite in [0,1].",
        ),
    ]
    raw_composite: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Pre-efficiency v5 score."),
    ] = None
    token_usage: PublicTokenUsage | None = None
    model_use: PublicModelUse | None = None
    token_efficiency: PublicTokenEfficiency | None = None
    composite_breakdown: PublicCompositeBreakdown | None = None
    v9_base: PublicV9BaseEvidence | None = None
    calibration_brier: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Advisory Brier score."),
    ] = None
    calibration_n: Annotated[int | None, Field(default=None, ge=0)] = None
    seed: Annotated[
        str,
        Field(
            pattern=r"^\d+$",
            description=(
                "Exact decimal dataset seed fixed after the miner committed the "
                "submission. Encoded as a string to avoid JavaScript integer rounding."
            ),
        ),
    ]
    run_size: Annotated[
        str | None,
        Field(
            default=None,
            pattern=r"^(small|medium|full)$",
            description="Generator profile used for the score, when recorded.",
        ),
    ]
    bench_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description="DittoBench version recorded with the score.",
        ),
    ]
    datagen_version: Annotated[
        str | None,
        Field(
            default=None,
            pattern=r"^v\d+\.\d+\.\d+$",
            description="Pinned dittobench-datagen module release for reproduction.",
        ),
    ]
    seed_source: Annotated[
        str,
        Field(
            pattern=r"^(on_chain|random_fallback|validator_local)$",
            description=(
                "Whether the post-commit seed was derived from an on-chain block, "
                "an unpredictable platform fallback, or chosen by the scoring "
                "validator because no per-submission dataset was pinned."
            ),
        ),
    ]
    dataset_sha256: Annotated[
        str | None,
        Field(
            default=None,
            pattern=r"^[0-9a-f]{64}$",
            description="Pinned hash of the exact generated dataset, when recorded.",
        ),
    ]
    accepted_at: Annotated[
        datetime, Field(description="When the platform accepted this score (UTC).")
    ]
    reproduction_command: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Copyable dittobench-datagen command pinned to the generator "
                "release used by the current benchmark."
            ),
        ),
    ]
    verification_command: Annotated[
        str | None,
        Field(
            default=None,
            description="Copyable command that prints the regenerated dataset hash.",
        ),
    ]
    case_results: Annotated[
        list[PublicCaseResult] | None,
        Field(
            default=None,
            description=(
                "Redacted per-question outcomes; answer keys and raw responses "
                "are never included."
            ),
        ),
    ]
    transcript_sha256: Annotated[
        str | None,
        Field(
            default=None,
            pattern=r"^[0-9a-f]{64}$",
            description=(
                "SHA-256 of this run's published transcript artifact (the "
                "graded per-case inputs), bound into the validator's score "
                "signature. The bytes live content-addressed in the public "
                "bucket at ``transcripts/{sha256}.json``; regenerating the "
                "dataset from the seed and re-running the public grader over "
                "the transcript reproduces this composite offline. Null when "
                "the validator published no transcript."
            ),
        ),
    ] = None


class PublicAgentSummary(BaseModel):
    """Glance-level state for opening one agent card.

    The full screening, score, and validator histories are intentionally absent.
    Clients can request ``PublicSubmissionPipeline`` concurrently while using
    this smaller response for the first paint.
    """

    generated_at: datetime
    agent_id: UUID
    miner_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Submitting miner's SS58 hotkey.")
    ]
    name: Annotated[str, Field(description="Miner-provided agent display name.")]
    name_handle: PublicNameHandle | None = Field(
        default=None,
        description=(
            "Signed handle reservation touching this name, when one exists. "
            "Same semantics as the public leaderboard annotation."
        ),
    )
    avatar_url: str | None = Field(
        default=None,
        description="Public URL for this miner's signed profile picture, if set.",
    )
    version: Annotated[int | None, Field(default=None, ge=1)] = None
    status: Annotated[str, Field(description="Current public lifecycle stage.")]
    submitted_at: datetime
    last_scored_at: datetime | None = None
    score_count: Annotated[int, Field(ge=0)]
    score_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description="Median composite across accepted current-benchmark scores.",
        ),
    ] = None
    quorum: Annotated[int, Field(ge=1)]
    screening_reason: str | None = None
    duplicate_of: UUID | None = None
    duplicate_name: str | None = None
    duplicate_version: Annotated[int | None, Field(default=None, ge=1)] = None
    duplicate_hotkey: Annotated[
        str | None, Field(default=None, pattern=_SS58_PATTERN)
    ] = None
    review_reason: str | None = None
    review_event: Literal["opened", "reopened", "cleared", "rejected"] | None = None
    review_event_at: datetime | None = None
    review_original_reason: str | None = None
    review_opened_at: datetime | None = None
    preserved_composite: Annotated[
        float | None, Field(default=None, ge=0.0, le=1.0)
    ] = None
    active_benchmarks: list[PublicBenchmarkProgress] = Field(default_factory=list)


class PublicSubmissionPipeline(BaseModel):
    """Full public execution history for one submitted agent."""

    generated_at: datetime
    agent_id: UUID
    status: str
    artifact_release: PublicArtifactRelease
    admission_retry: PublicAdmissionRetry | None = Field(
        default=None,
        description=(
            "Live admission-retry state while the submission is still in "
            "build & admission; null once admission is terminal."
        ),
    )
    submission_family: PublicSubmissionFamily | None = Field(
        default=None,
        description=(
            "Current-benchmark submission family when this agent shares a "
            "leaderboard ownership slot with another finalized generation."
        ),
    )
    active_bench_version: Annotated[
        int, Field(ge=1, description="Benchmark version currently being scored.")
    ]
    score_bench_version: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Benchmark version ``score_count`` and ``final_composite`` are "
                "counted against: the era this submission belongs to. Equal to "
                "``active_bench_version`` for current-generation submissions, "
                "and older for one whose generation has closed."
            ),
        ),
    ]
    score_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Accepted independent scores in ``score_bench_version``, not in "
                "the active version. Scores from different eras are not "
                "comparable, so this never mixes them."
            ),
        ),
    ]
    quorum: Annotated[int, Field(ge=1)]
    score_floor: Annotated[
        float,
        Field(
            ge=0.0,
            # Same scale, and therefore the same bound, as the board's
            # ``official_composite``: this IS one of those numbers, so it
            # inherits the historical multiplicative bonus that can carry a
            # ranking score above raw 1.0. Capping it at 1.0 here did not keep
            # the floor in range -- it 500'd the whole endpoint for every agent
            # the moment fifth place crossed, because the floor is global.
            le=1.1,
            description=(
                "Current finalized fifth-place score used for safe continuation "
                "after two scores; 0 when fewer than five ranked miners exist.\n\n"
                "On the ``official_composite`` scale, so a legacy multiplicative "
                "bonus can put it above 1.0 while raw composites cannot.\n\n"
                "This is the fifth-highest finalized ``official_composite`` in "
                "``active_bench_version`` -- the same score, in the same order, "
                "that the public leaderboard's ``rank`` and the validator "
                "weight fold read, so it IS the score of the finalized row the "
                "board ranks fifth. ``score_floor_agent_id`` names that row. "
                "(The board interleaves pre-quorum provisional entries, which "
                "hold no floor, so the fifth row *displayed* need not be the "
                "fifth finalized one.)"
            ),
        ),
    ]
    score_floor_agent_id: Annotated[
        UUID | None,
        Field(
            default=None,
            description=(
                "The agent whose ``official_composite`` IS ``score_floor``, so the "
                "quoted number can be checked against "
                "/public/agent/{id}/scores. Null when the era has fewer than "
                "five ranked agents and no floor applies."
            ),
        ),
    ] = None
    score_floor_agent_name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Human-friendly name of the agent holding ``score_floor``. Null "
                "whenever ``score_floor_agent_id`` is null."
            ),
        ),
    ] = None
    score_floor_agent_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Submission version of the agent holding ``score_floor``; null "
                "for a legacy upload with no version, or when no floor applies."
            ),
        ),
    ] = None
    provisional_scores: list[PublicProvisionalScore] = Field(default_factory=list)
    confirmation_scores: list[PublicConfirmationScore] = Field(default_factory=list)
    final_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "Canonical median over the ``score_bench_version`` scores once "
                "quorum is reached; null while scores are still provisional."
            ),
        ),
    ]
    screening_attempts: list[PublicScreeningAttempt] = Field(default_factory=list)
    validation_attempts: list[PublicValidationAttempt] = Field(default_factory=list)
    inference_runs: list[PublicInferenceRun] = Field(default_factory=list)
    dispute: PublicScreeningDispute | None = None


class PublicDatasetReveal(BaseModel):
    """The full labeled dataset a finalized submission was scored against.

    Regenerated from the submission's published (on-chain-derived) seed, so anyone
    can **independently re-grade** the k=3 scores: the ``artifact`` carries the
    complete DatasetArtifact including the answer keys (expected tools/answers).
    Safe to publish because the seed is one-time and unpredictable, so revealing a
    past submission's answers cannot help overfit any future (differently-seeded)
    run. ``dataset_sha256`` is re-verified to match what was pinned at scoring, so
    the revealed bytes provably are the scored dataset.
    """

    agent_id: Annotated[UUID, Field(description="The scored agent's id.")]
    miner_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Submitting miner's SS58 hotkey.")
    ]
    seed: Annotated[int, Field(description="Dataset seed (on-chain derived).")]
    run_size: Annotated[
        str, Field(description="Generator profile (small|medium|full).")
    ]
    dataset_sha256: Annotated[
        str, Field(description="SHA-256 of the artifact, verified against the pin.")
    ]
    bench_version: Annotated[
        int | None,
        Field(default=None, description="Benchmark version of the artifact."),
    ]
    dataset_seed_block: Annotated[
        int | None,
        Field(default=None, description="On-chain block the seed was derived from."),
    ]
    dataset_seed_block_hash: Annotated[
        str | None, Field(default=None, description="Hash of the seed block.")
    ]
    artifact: Annotated[
        dict[str, Any],
        Field(
            description=(
                "The full labeled DatasetArtifact (tool + memory cases, seeding "
                "waves, fixtures, AND the answer keys) so the score is "
                "independently reproducible."
            )
        ),
    ]


class PublicBenchCorpusEntry(BaseModel):
    """One scored run of a retired benchmark, with its FULL answer key.

    Part of the retired-version corpus release: because a retired benchmark is
    never scored again, its per-case answer keys (``expected`` tools/answers,
    ``called``, ``case_id``) carry zero anti-overfit cost and are published
    verbatim from ``scores.details`` so researchers get the complete labeled
    benchmark.
    """

    agent_id: Annotated[UUID, Field(description="The scored agent's id.")]
    miner_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Submitting miner's hotkey.")
    ]
    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Scoring validator's hotkey.")
    ]
    seed: Annotated[int, Field(description="Dataset seed for the run.")]
    run_id: Annotated[str, Field(description="Scoring-engine run id.")]
    composite: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Composite this validator reported in [0,1].",
        ),
    ]
    per_case: Annotated[
        list[dict[str, Any]],
        Field(
            default_factory=list,
            description=(
                "Full UNREDACTED per-case records, answer keys included (retired "
                "version, so safe). Empty when the run stored no per-case data."
            ),
        ),
    ]


class PublicBenchCorpusResponse(BaseModel):
    """A page of a retired benchmark's full labeled corpus.

    Served only for a retired ``bench_version`` (``< current``); the live version
    is refused (409) since exposing its answer keys would be an overfit vector.
    Paginate with ``limit`` / ``offset`` up to ``total``.
    """

    bench_version: Annotated[int, Field(description="The retired benchmark version.")]
    generated_at: Annotated[
        datetime, Field(description="When this page was read (UTC).")
    ]
    count: Annotated[int, Field(ge=0, description="Entries in this page.")]
    total: Annotated[int, Field(ge=0, description="Total runs for this version.")]
    limit: Annotated[int, Field(ge=1, description="Page size.")]
    offset: Annotated[int, Field(ge=0, description="Page offset.")]
    entries: Annotated[
        list[PublicBenchCorpusEntry],
        Field(default_factory=list, description="Scored runs with full answer keys."),
    ]


class PublicAuditEntry(BaseModel):
    """One entry of the append-only, hash-chained public score audit log.

    Each entry records a scoring event verbatim: a validator's signed ``score``,
    a ``score_retest_requested`` operator decision that keeps the signed score
    canonical while a replacement runs, a ``score_invalidated`` atomic swap,
    a ``score_retest_released`` cancellation, or an ``agent_finalized`` event
    (quorum reached, the median + scoring validators).
    ``entry_hash`` is the SHA-256 of the entry's canonical content (which embeds
    ``prev_hash``); ``prev_hash`` links to the previous entry's ``entry_hash``.
    A consumer replays the feed and recomputes each hash to prove the sequence
    was never reordered, edited, or truncated.
    """

    seq: Annotated[int, Field(ge=1, description="Monotonic append order.")]
    agent_id: Annotated[UUID, Field(description="Agent the event is about.")]
    validator_hotkey: Annotated[
        str | None,
        Field(default=None, description="Scoring validator (null on finalize)."),
    ]
    event: Annotated[
        str,
        Field(
            description=(
                'Event kind such as "score", "score_retest_requested", '
                '"score_invalidated", "score_retest_released", '
                '"agent_finalized", or "transform_audit".'
            )
        ),
    ]
    payload: Annotated[
        dict[str, Any],
        Field(description="Event content (the hash preimage's payload field)."),
    ]
    prev_hash: Annotated[
        str, Field(description="Previous entry's entry_hash (hex); genesis = 64 zeros.")
    ]
    entry_hash: Annotated[
        str, Field(description="SHA-256 (hex) of this entry's canonical content.")
    ]
    recorded_at: Annotated[
        datetime, Field(description="When the platform appended the entry (UTC).")
    ]


class PublicAuditResponse(BaseModel):
    """A page of the public audit feed, oldest first, with the chain root.

    Paginate by ``seq``: replay from ``since_seq=0`` and re-request with the last
    ``seq`` seen to stream new entries. ``genesis_hash`` is the ``prev_hash`` of
    the very first entry, so a consumer can verify the chain from the root.
    """

    generated_at: Annotated[
        datetime, Field(description="When this page was read (UTC).")
    ]
    count: Annotated[int, Field(ge=0, description="Entries in this page.")]
    genesis_hash: Annotated[
        str, Field(description="The chain root (first entry's prev_hash).")
    ]
    head_hash: Annotated[
        str | None,
        Field(default=None, description="entry_hash of the last entry in this page."),
    ]
    entries: Annotated[
        list[PublicAuditEntry],
        Field(default_factory=list, description="Entries with seq > since_seq."),
    ]


class PublicHealthResponse(BaseModel):
    """Aggregate subnet-health rollup for the public dashboard.

    Derived only from what the platform records (submissions + reported scores).
    Run started/failed counts, set-weights latency and per-stage timings are
    validator-side telemetry (wandb), not served here; the platform only ever
    sees a *successful* score, so it deliberately reports no "success rate".
    """

    generated_at: Annotated[
        datetime, Field(description="When this snapshot was read (UTC).")
    ]
    miners: Annotated[
        int, Field(ge=0, description="Distinct miners who have ever submitted.")
    ]
    scored_miners: Annotated[
        int, Field(ge=0, description="Distinct miners on the leaderboard (scored).")
    ]
    scored_agents: Annotated[
        int, Field(ge=0, description="Agents currently eligible (scored).")
    ]
    last_scored_at: Annotated[
        datetime | None,
        Field(default=None, description="When a validator last scored anything (UTC)."),
    ]
    total_scores: Annotated[
        int, Field(ge=0, description="All validator score records ever recorded.")
    ]
    scores_24h: Annotated[
        int, Field(ge=0, description="Scores generated in the last 24h.")
    ]
    avg_latency_ms: Annotated[
        int | None,
        Field(
            default=None, ge=0, description="Mean per-score median case latency (ms)."
        ),
    ]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "generated_at": "2026-07-04T12:00:00Z",
                "miners": 12,
                "scored_miners": 5,
                "scored_agents": 7,
                "last_scored_at": "2026-07-04T11:52:00Z",
                "total_scores": 18,
                "scores_24h": 9,
                "avg_latency_ms": 812,
            }
        }
    )


FleetAvailability = Literal["available", "stale", "offline", "paused", "unknown"]
# ``critical`` is reserved for a validator that cannot do the one job it exists
# to do. A scorer that is not serving belongs there and not next to a stalled
# disk: both were ``warning`` before, and the fleet view could not tell an
# inconvenience from a validator taking leases it can never complete.
FleetHealth = Literal["healthy", "warning", "critical", "unknown"]
# Whether a validator can serve the benchmark the fleet is actually scoring.
# Not a health rollup and not a liveness claim: it is the leasing gate's own
# answer, kept separate because a validator that cannot serve the active
# benchmark is issued no work at all -- the one failure that host metrics, a
# fresh heartbeat and a green scorer probe can all coexist with.
#
# ``serving``            clears the gate for the active benchmark.
# ``scorer_unverified``  software new enough to advertise it, scorer not
#                        advertising it (unverified identity, missing
#                        calibration, or simply a narrower version set).
# ``software_obsolete``  a heartbeat protocol that cannot describe the active
#                        benchmark at all. Only an upgrade changes this.
BenchServiceability = Literal["serving", "scorer_unverified", "software_obsolete"]
# Whether the validator's scorer actually answered its capability probe.
# ``unreported`` covers both a validator too old to carry probe evidence and one
# that carried none; it is never a claim that the scorer is fine.
ScorerLiveness = Literal["serving", "degraded", "not_serving", "unreported"]
ValidatorAssignmentState = Literal[
    # The validator is doing exactly the work the platform leased it.
    "synchronized",
    # The lease was issued too recently for the validator to have reported it yet
    # (a normal job hand-off). A transient, non-alarming state, kept separate so
    # the fleet view does not flap red between jobs.
    "assigning",
    # The validator has gone quiet (no fresh heartbeat within the online window).
    # A liveness problem, distinct from a job/assignment problem.
    "heartbeat_stale",
    # The validator is heartbeating but, past the hand-off grace, is not doing its
    # assigned job (or is reporting work the platform never assigned). A real
    # job/assignment mismatch. (Renamed from the misleading "heartbeat_mismatch".)
    "assignment_mismatch",
    # No live lease and no reported work.
    "unassigned",
]


class PublicSystemMetrics(BaseModel):
    """Coarse allowlisted metrics; collector timestamps stay private."""

    cpu_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    memory_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    disk_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    docker_status: Literal["healthy", "degraded", "unavailable"]
    running_containers: Annotated[int, Field(ge=0, le=1000)]
    unhealthy_containers: Annotated[int, Field(ge=0, le=1000)]


# What the platform can see about a slot whose lease it evicted while the
# validator's container kept executing. Deliberately three-valued minus the
# fourth: ``released`` never reaches the wire, because a released slot is
# genuinely idle and must keep rendering as idle.
#
# ``still_running``  the validator's own signed occupancy claim still lists this
#                    slot, held by the evicted agent. Positive evidence.
# ``indeterminate``  the platform cannot tell. Most often a heartbeat protocol
#                    below 16, which omits a claimed-but-quiet slot entirely and
#                    so cannot distinguish "the container exited" from "the
#                    container is between progress reports".
OrphanedSlotState = Literal["still_running", "indeterminate"]


class PublicOrphanedSlot(BaseModel):
    """A slot the platform released out from under a still-executing benchmark.

    An operator eviction ends the platform's half of a lease at once; the
    validator's container runs to completion and has its late score refused with
    a 409. For that window the host is doing a full benchmark's worth of work
    that cannot produce a score, and before this existed every such slot rendered
    as `Idle` -- which is how a fleet with no headroom reads as a fleet with
    plenty.

    Derived, never asserted: see `ditto.db.queries.orphaned_leases`. `reason`
    names the observation behind `state`, so an `indeterminate` row can say
    whether the platform is blind because the validator is too old to answer or
    because its heartbeat is stale.
    """

    slot_id: Annotated[str, Field(description="Which slot, e.g. `slot-0`.")]
    agent_id: UUID
    agent_name: str | None = None
    bench_version: Annotated[int, Field(ge=1)]
    state: OrphanedSlotState
    reason: Annotated[
        str,
        Field(
            description=(
                "The observation behind `state` (e.g. "
                "`validator_still_claims_slot`, "
                "`pre_v16_reporter_omits_a_quiet_slot`). Plain string rather "
                "than an enum: a new evidence code must never turn an "
                "operator's read into a client error."
            )
        ),
    ]
    evicted_at: Annotated[
        datetime, Field(description="When the operator eviction released the lease.")
    ]
    orphaned_for_seconds: Annotated[
        float,
        Field(
            ge=0,
            description="How long the run has been executing without a lease.",
        ),
    ]
    original_deadline: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "The deadline the evicted lease would otherwise have run to. "
                "These runs are expected to self-terminate by roughly this "
                "time; null when the audit row records no readable deadline."
            ),
        ),
    ]
    protocol_version: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Heartbeat protocol of the report this was derived from, or "
                "null when there is no heartbeat row. Below 16 a validator "
                "omits a claimed-but-quiet slot, which is why `state` is often "
                "`indeterminate` there."
            ),
        ),
    ]


class PublicClaimedSlot(BaseModel):
    """One slot the validator signed as busy, before ticket confirmation."""

    slot_id: str
    agent_id: UUID


class PublicValidatorSlotPolicy(BaseModel):
    """The operator slot policy ticket dispatch is applying to the fleet.

    Reported alongside the heartbeats so a fleet view can say why a validator
    advertising eight slots is only ever handed six, instead of presenting
    advertised capacity as if it were available capacity.
    """

    max_concurrent_slots: Annotated[
        int,
        Field(
            ge=1,
            le=8,
            description=(
                "Most benchmark slots the platform will hold live tickets on for "
                "any ONE validator, whatever it advertises."
            ),
        ),
    ]
    disk_percent_ceiling: Annotated[
        int,
        Field(
            ge=0,
            le=100,
            description=(
                "Host disk utilization at or above which a validator is held to a "
                "single slot until a fresh heartbeat reports headroom; zero means "
                "disk gating is disabled."
            ),
        ),
    ]


class PublicValidatorHeartbeat(BaseModel):
    """Latest signed software report from one permitted validator."""

    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Validator's public hotkey.")
    ]
    software_version: str
    protocol_version: Annotated[int, Field(ge=1)]
    state: ValidatorRuntimeState
    assigned_agent_id: UUID | None = None
    assigned_agent_name: str | None = None
    reported_agent_id: UUID | None = None
    assignment_state: ValidatorAssignmentState
    active_agent_id: UUID | None = None
    active_benchmark: PublicBenchmarkProgress | None = None
    configured_slots: Annotated[int, Field(ge=1, le=8)] = 1
    allowed_slots: Annotated[
        int,
        Field(
            ge=0,
            le=8,
            description=(
                "How many of the advertised slots dispatch will actually fund "
                "right now: zero while exact-validator issuance is paused, the "
                "operator cap narrowing `configured_slots`, or one while this "
                "validator's resource ceiling is tripped. Advertised capacity "
                "above this never receives a ticket."
            ),
        ),
    ] = 1
    issuance_paused: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Whether Backroom's exact-validator brake currently refuses new "
                "canonical, continual-retest, and confirmation leases for this "
                "hotkey. Already-issued leases may still finish and report."
            ),
        ),
    ]
    healthy_slots: list[str] = Field(default_factory=lambda: ["slot-0"])
    admission: BenchmarkAdmission = "accepting"
    active_benchmarks: list[PublicBenchmarkProgress] = Field(default_factory=list)
    assigned_benchmarks: list[PublicBenchmarkProgress] = Field(default_factory=list)
    confirmation_benchmarks: Annotated[
        list[PublicConfirmationProgress],
        Field(
            default_factory=list,
            description=(
                "Live LongMemEval and ablation confirmation tickets. These use "
                "independent longmem slots and never consume ordinary benchmark "
                "capacity."
            ),
        ),
    ]
    orphaned_slots: Annotated[
        list[PublicOrphanedSlot],
        Field(
            default_factory=list,
            description=(
                "Slots whose lease an operator evicted while the validator's "
                "benchmark container may still be executing. Empty in the "
                "ordinary case. A slot listed here is NOT free: treat it as "
                "occupied when reasoning about fleet headroom, even in the "
                "`indeterminate` state."
            ),
        ),
    ]
    claimed_slots: Annotated[
        list[PublicClaimedSlot],
        Field(
            default_factory=list,
            description=(
                "Signed occupancy the validator advertised before ticket "
                "confirmation. A slot listed here but missing from "
                "active_benchmarks is occupied locally and unconfirmed on the "
                "ledger — the awaiting-progress zombie shape."
            ),
        ),
    ]
    first_seen_at: datetime | None = None
    reported_at: datetime
    seen_at: datetime
    online: bool
    availability: FleetAvailability
    health: FleetHealth
    scorer_liveness: Annotated[
        ScorerLiveness,
        Field(
            default="unreported",
            description=(
                "Whether the validator's scorer answered its `/v1/capabilities` "
                "probe: `serving`, `degraded` (answered, part of the reply "
                "rejected), `not_serving` (no usable answer), or `unreported` "
                "(no probe evidence on this heartbeat). Requires heartbeat "
                "protocol 15; older validators always read `unreported`."
            ),
        ),
    ]
    health_reasons: Annotated[
        list[str],
        Field(
            default_factory=list,
            description=(
                "Detailed labels explaining a non-healthy `health` badge (e.g. "
                "'dittobench_api: degraded', 'benchmark stalled'). Empty when "
                "healthy. Intended for a badge tooltip so the summary stays compact "
                "without hiding the reason."
            ),
        ),
    ]
    bench_serviceability: Annotated[
        BenchServiceability,
        Field(
            description=(
                "Whether this validator can serve the `active_bench_version` of "
                "the same response, and if not, why. This is the gate the "
                "platform itself applies before leasing work, so anything other "
                "than `serving` means the validator is issued nothing and cannot "
                "earn a score however healthy its host metrics read. "
                "`software_obsolete` is a heartbeat protocol that cannot "
                "describe the active benchmark at all — no probe result and no "
                "restart changes it, only an upgrade. `scorer_unverified` is "
                "current-enough software whose scorer is not advertising the "
                "active benchmark, which a fix can clear. Judged on capability "
                "alone: a quiet or offline validator that still advertises the "
                "active benchmark reads `serving`."
            ),
        ),
    ]
    system_metrics: PublicSystemMetrics | None = None
    capabilities: ValidatorCapabilities | None = None
    stack: ValidatorStackIdentity | None = None
    stack_health: ValidatorStackHealth | None = None
    updater_status: Annotated[
        ValidatorUpdaterStatus | None,
        Field(
            default=None,
            description=(
                "Signed sanitized managed-updater state. Null for validators "
                "older than heartbeat protocol v23."
            ),
        ),
    ] = None


class PublicValidatorHeartbeatsResponse(BaseModel):
    """Public view of validators that run heartbeat-capable software."""

    generated_at: datetime
    online_window_seconds: Annotated[int, Field(ge=1)]
    stale_window_seconds: Annotated[int, Field(ge=1)]
    # Carried here as well as on the operations snapshot so `serves_active_bench`
    # is readable on its own: a client polling only this route can say which
    # benchmark a validator is being judged against.
    active_bench_version: Annotated[int, Field(ge=1)]
    # Fleet-wide, so a reader can tell an idle slot ("nothing to run") from a
    # slot the operator has capped ("nothing will be run here") without
    # reimplementing the policy from the per-validator numbers.
    slot_policy: PublicValidatorSlotPolicy
    reported_count: Annotated[int, Field(ge=0)]
    online_count: Annotated[int, Field(ge=0)]
    validators: list[PublicValidatorHeartbeat] = Field(default_factory=list)


class PublicRolloutQueueEntry(BaseModel):
    """One inherited submission waiting on the desired benchmark rollout."""

    agent_id: Annotated[UUID, Field(description="The inherited agent's id.")]
    miner_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Submitting miner's SS58 hotkey.")
    ]
    name: Annotated[str, Field(description="Miner-provided agent display name.")]
    version: Annotated[int | None, Field(default=None, ge=1)] = None
    submitted_at: datetime
    bench_version: Annotated[int, Field(ge=1)]
    position: Annotated[
        int,
        Field(
            ge=1,
            description="Frozen one-based position in the rollout cohort.",
        ),
    ]
    status: Literal["waiting_validator", "evaluating"]
    score_count: Annotated[int, Field(ge=0)]
    quorum: Annotated[int, Field(ge=1)]
    retry_state: RetryState | None = None
    retry_after: datetime | None = None
    active_benchmarks: list[PublicBenchmarkProgress] = Field(default_factory=list)


class PublicSubmissionImageBuild(BaseModel):
    """Public-safe provenance for one attempt-bound miner image build."""

    agent_id: UUID
    agent_name: str
    agent_version: Annotated[int | None, Field(default=None, ge=1)] = None
    status: Literal[
        "queued",
        "leased",
        "running",
        "succeeded",
        "fallback_required",
        "canceled",
        "consumed",
    ]
    provider: Literal["targon", "gcp", "hetzner"] | None = None
    attempt_count: Annotated[int, Field(ge=0, le=3)]
    output_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    output_size_bytes: Annotated[int | None, Field(ge=1, le=4294967296)] = None
    error_code: Annotated[str | None, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    consumed_at: datetime | None = None
    updated_at: datetime


class PublicSubmissionImageBuildSnapshot(BaseModel):
    """Recent Targon-first miner build activity for the operations board."""

    window_hours: Annotated[int, Field(ge=1, le=168)]
    active_count: Annotated[int, Field(ge=0)]
    targon_completed_count: Annotated[int, Field(ge=0)]
    fallback_authorized_count: Annotated[int, Field(ge=0)]
    builds: list[PublicSubmissionImageBuild] = Field(default_factory=list)


class PublicOperationsResponse(BaseModel):
    """One cacheable operations snapshot shared by pipeline and fleet views."""

    generated_at: datetime
    active_bench_version: Annotated[int, Field(ge=1)]
    desired_bench_version: Annotated[int, Field(ge=1)]
    benchmark_rollout_status: Literal[
        "inactive", "collecting", "blocked_ineligible", "activated", "superseded"
    ]
    activity: PublicActivityResponse
    rollout_queue: list[PublicRolloutQueueEntry] = Field(default_factory=list)
    validators: PublicValidatorHeartbeatsResponse
    submission_builds: PublicSubmissionImageBuildSnapshot


class PublicValidatorName(BaseModel):
    """Optional public chain metadata paired with a validator identity."""

    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Validator's public hotkey.")
    ]
    display_name: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    stake_weight: Annotated[float, Field(ge=0)] | None = None


class PublicValidatorNamesResponse(BaseModel):
    """Non-blocking snapshot of optional Taostats display-name decoration."""

    generated_at: datetime
    source: Literal["taostats"] = "taostats"
    status: Literal["disabled", "fresh", "stale", "unavailable"]
    refreshed_at: datetime | None = None
    validators: list[PublicValidatorName] = Field(default_factory=list)


class PublicScreenerProgress(BaseModel):
    """Allowlisted stage and signed start time for one current job."""

    stage: ScreenerProgressStage
    started_at: datetime


class PublicHostSpecs(BaseModel):
    """Hardware a screener announced about itself, not what it is doing."""

    cpu_count: Annotated[int, Field(ge=1, le=1024)]
    cpu_physical_cores: Annotated[int, Field(ge=1, le=1024)] | None = None
    memory_total_mib: Annotated[int, Field(ge=1, le=1 << 24)]
    disk_total_gib: Annotated[int, Field(ge=1, le=1 << 20)]
    architecture: str


class PublicScreenerHeartbeat(BaseModel):
    """Latest public-safe report from one authenticated screener instance."""

    instance_id: Annotated[
        str,
        Field(
            description="Per-worker instance id (fleet shares one hotkey).",
        ),
    ]
    screener_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Screener's public hotkey.")
    ]
    provider: Literal["gcp", "targon", "hetzner", "home", "test"] | None = None
    node_status: Literal["active", "draining", "quarantined", "revoked"] | None = None
    capacity: Annotated[int, Field(ge=1, le=16)] = 1
    software_version: str
    protocol_version: Annotated[int, Field(ge=1)]
    policy_version: Annotated[int, Field(ge=1)]
    state: ScreenerRuntimeState
    active_agent_id: UUID | None = None
    active_agent_name: str | None = None
    screening_progress: PublicScreenerProgress | None = None
    first_seen_at: datetime | None = None
    reported_at: datetime
    seen_at: datetime
    online: bool
    availability: FleetAvailability
    health: FleetHealth
    system_metrics: PublicSystemMetrics | None = None
    host_specs: PublicHostSpecs | None = None


class PublicScreenerHeartbeatsResponse(BaseModel):
    """Public view of authenticated platform-operated screeners."""

    generated_at: datetime
    online_window_seconds: Annotated[int, Field(ge=1)]
    stale_window_seconds: Annotated[int, Field(ge=1)]
    reported_count: Annotated[int, Field(ge=0)]
    online_count: Annotated[int, Field(ge=0)]
    screeners: list[PublicScreenerHeartbeat] = Field(default_factory=list)


class BenchHarnessConfig(BaseModel):
    """How the harness model is frozen for the current benchmark version."""

    locked: bool = Field(description="Every harness is scored against ONE model.")
    canonical_id: str = Field(
        description="Canonical locked model id (docs + score reports)."
    )
    serving: str = Field(
        description=(
            "The serving route for the locked model; providers may be selected "
            "dynamically."
        )
    )
    thinking: bool = Field(
        description="Whether the benchmark model's locked reasoning mode is enabled."
    )
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description=(
            "Locked provider-independent reasoning effort, or null when reasoning "
            "is disabled for the benchmark version."
        ),
    )
    enforcement: str = Field(description="How the lock is enforced around the sandbox.")


class BenchGradingConfig(BaseModel):
    """How runs are graded."""

    judge_free: bool = Field(description="No LLM judge anywhere in scoring.")
    grader: str = Field(description="The public grader module.")
    description: str = Field(description="One-line grading summary.")


class BenchDatasetConfig(BaseModel):
    """How datasets are generated and pinned."""

    generator: str = Field(description="The public generator module.")
    seed_derivation: str = Field(description="Where a scored run's seed comes from.")
    reproduce: str = Field(
        description="The command reproducing any scored dataset byte-for-byte."
    )


class PublicCategoryDoc(BaseModel):
    """What one scored test category checks (never the answer key)."""

    key: Annotated[str, Field(description="Exact category slug the scorer surfaces.")]
    label: Annotated[str, Field(description="Human-readable name.")]
    kind: Annotated[
        str,
        Field(description="memory | conversational | tool | multi_step | integrity."),
    ]
    purpose: Annotated[
        str, Field(description="One public-safe sentence: what it probes.")
    ]
    example: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "A short, public-safe illustration of what the case looks like "
                "(a representative user turn and any minimal setup). Never an "
                "answer key or per-seed content; uses generic placeholders."
            ),
        ),
    ] = None


class PublicMetricDoc(BaseModel):
    """What one headline metric or composite-gate factor means."""

    key: Annotated[str, Field(description="Metric / gate-factor key.")]
    label: Annotated[str, Field(description="Human-readable name.")]
    description: Annotated[
        str, Field(description="How it is computed and what it affects.")
    ]


class PublicBenchVersionDoc(BaseModel):
    """What one immutable bench_version is and what it changed vs the previous one."""

    version: int
    epoch: Annotated[str, Field(description="Contract publication date (YYYY-MM-DD).")]
    title: Annotated[str, Field(description="Short name of the release.")]
    summary: Annotated[
        str, Field(description="One-paragraph description of the version.")
    ]
    highlights: Annotated[
        list[str],
        Field(default_factory=list, description="The version's headline changes."),
    ]


class PublicBenchGlossaryResponse(BaseModel):
    """Every scored category and every metric / gate factor explained, plus the
    bench_version changelog, so miners understand exactly what a score reflects and
    what changed between benchmark versions (``GET /public/bench/glossary``).

    Purposes describe what each case probes and how each metric is computed; no
    answer keys or per-case content are ever exposed. This is the programmatic
    companion to the on-dashboard glossaries and the composite breakdown.
    """

    bench_version: int
    categories: list[PublicCategoryDoc]
    metrics: list[PublicMetricDoc]
    versions: Annotated[
        list[PublicBenchVersionDoc],
        Field(
            default_factory=list,
            description="The bench_version changelog, newest first.",
        ),
    ]


class PublicBenchConfigResponse(BaseModel):
    """The current benchmark setup (``GET /public/bench/config``).

    Everything here is a consensus parameter or a public fact: the frozen
    harness model, the judge-free grading rules, and the seed/dataset
    reproducibility story. Values change only with coordinated fleet bumps
    (and a bench_version change when scoring-affecting).
    """

    bench_version: int
    desired_bench_version: int | None = Field(
        default=None,
        description=(
            "A newer contract currently collecting qualification scores, or null. "
            "bench_version and harness always describe the active scoring authority."
        ),
    )
    harness: BenchHarnessConfig
    grading: BenchGradingConfig
    dataset: BenchDatasetConfig
    public_mirror_url_template: str | None = Field(
        description=(
            "Anonymous-read URL template for finalized run records "
            "(dataset pin + k=3 signed scores), or null when mirroring is off."
        )
    )
    public_transcript_url_template: str | None = Field(
        default=None,
        description=(
            "Public URL template for content-addressed run transcripts "
            "(``{sha256}`` = a score's signature-bound ``transcript_sha256``), "
            "or null when transcript publication is unavailable."
        ),
    )
    public_transcript_telemetry_url_template: str = Field(
        description=(
            "Same-origin URL template for the digest-verified, allowlisted run "
            "telemetry projection. This never returns transcript content."
        )
    )
    ledger_path: str = Field(description="The self-verifying signed score ledger.")
    generated_at: datetime


class PublicBenchRolloutMember(BaseModel):
    """One frozen-cohort agent's progress toward the desired ``bench_version``."""

    agent_id: str
    position: int
    score_count: int = Field(
        description="Accepted scores on the desired version, out of the quorum."
    )
    currently_top_five: bool


class PublicBenchRolloutResponse(BaseModel):
    """Benchmark-version rollout state (``GET /public/bench/rollout``).

    Two versions matter here and they are not the same number:
    ``active_version`` is the one that currently drives on-chain weights, and
    ``desired_version`` is the one being rolled out. The whole ledger switches
    at once, and only once ``ranked_quorum_agents`` reaches
    ``min_ranked_quorum_agents``: that gate is what guarantees the emission set
    (champion plus tail) is never short at the moment authority moves.

    Extra keys are preserved rather than dropped: this model documents the shape
    without becoming a filter on it.
    """

    model_config = ConfigDict(extra="allow")

    active_version: int = Field(
        description="The bench_version that currently determines chain weights."
    )
    desired_version: int = Field(
        description="The bench_version being rolled out (equal to active when idle)."
    )
    status: str = Field(
        description="inactive | collecting | superseded | activated | blocked."
    )
    blocked_reason: str | None = None
    capability_bench_version: int
    ranked_quorum_agents: int | None = Field(
        default=None,
        description=(
            "How many eligible agents hold a complete RANKED quorum at "
            "desired_version: a full-benchmark median row with a positive "
            "composite, not merely a row count. The authority switch is gated "
            "on this reaching min_ranked_quorum_agents."
        ),
    )
    min_ranked_quorum_agents: int | None = Field(
        default=None,
        description=(
            "The threshold ranked_quorum_agents must reach before weights move "
            "to desired_version. Read this rather than hardcoding it."
        ),
    )
    canary_capable_validator_count: int
    v3_capable_validator_count: int = Field(
        description=(
            "DEPRECATED alias of canary_capable_validator_count. Kept because it "
            "is public API; read the new key."
        )
    )
    current_hybrid_top_five: list[str] = Field(default_factory=list)
    qualification_converged: bool = False
    cohort_size: int = Field(
        default=0,
        description="Frozen inherited rescore cohort size, capped at 25.",
    )
    cohort_ready_count: int = Field(
        default=0,
        description="Cohort members with a complete desired-version quorum.",
    )
    priority_cohort_size: int = Field(
        default=5,
        description="Inherited leaders that must finish before later cohort work.",
    )
    priority_complete: bool = Field(
        default=False,
        description="Whether the fleet-wide first-five scoring barrier is closed.",
    )
    members: list[PublicBenchRolloutMember] = Field(default_factory=list)
    qualification_blockers: list[dict[str, str]] = Field(default_factory=list)
