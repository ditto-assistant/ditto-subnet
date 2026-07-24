"""Wire shapes for the ``/validator/*`` endpoints.

These back the validator daemon's epoch loop against the platform:

1. ``GET  /validator/queue`` — list agents awaiting evaluation.
2. ``GET  /validator/agent/{id}/artifact`` — fetch a download URL for the
   uploaded tarball so the daemon can run it through the harness.
3. ``POST /validator/agent/{id}/score`` — report a DittoBench
   :class:`ScoreReport` back to the platform once scoring completes.

The platform stays thin: the validator daemon owns the chain identity and
drives the scoring engine (`dittobench-api`) itself. It only reads work
from here and writes scores back; weight-setting happens on the daemon via
``ChainClient.put_weights``.

``ScoreReport`` / ``CaseScore`` mirror the DittoBench Go validator wire
contract (see ``dittobench-api`` ``pkg/protocol`` and the starter kit's
``PROTOCOL.md``) so a report produced by the scoring engine round-trips
through this endpoint unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.benchmark_progress import BenchmarkProgress
from ditto.api_models.inference import InferenceGrantOffer
from ditto.api_models.stack_health import ValidatorStackHealth
from ditto.api_models.system_health import SystemMetrics
from ditto.api_models.upload import (
    _SIGNATURE_HEX_PATTERN,
    _SS58_PATTERN,
)
from ditto.api_models.validator_capabilities import (
    ValidatorCapabilities,
    ValidatorStackIdentity,
)

_CODE_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_SOFTWARE_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"

ValidatorRuntimeState = Literal[
    "polling",
    "running_benchmark",
    "updating_weights",
    "idle",
    "error",
    "paused",
]

# Coarse, source-free classification of why a validator is handing a leased
# ticket back before its deadline. Deliberately narrow: it never carries the
# miner's error detail, only whether the fault was the validator's own scoring
# infrastructure or an ordinary scoring failure.
FailJobReason = Literal["infrastructure", "scoring_error", "sandbox_oom"]


class JobRequest(BaseModel):
    """Fresh, one-time signed request to claim a scoring ticket."""

    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Claiming validator hotkey.")
    ]
    slot_id: Annotated[str | None, Field(pattern=r"^slot-[0-7]$")] = None
    nonce: Annotated[UUID, Field(description="One-time claim nonce.")]
    requested_at: Annotated[
        datetime, Field(description="UTC time at which the claim was signed.")
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description="sr25519 signature over the canonical claim payload.",
        ),
    ]

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must include a timezone")
        return value


class Top5ConfirmationJobRequest(BaseModel):
    """Fresh signed claim for a member of the top-5 shared-seed rescore lane."""

    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Claiming validator hotkey.")
    ]
    champion_agent_id: Annotated[
        UUID, Field(description="Current KOTH incumbent (the CRN seed anchor).")
    ]
    member_agent_id: Annotated[
        UUID,
        Field(description="Emission-set member (champion or tail) to rescore."),
    ]
    nonce: Annotated[UUID, Field(description="One-time claim nonce.")]
    requested_at: Annotated[
        datetime, Field(description="UTC time at which the claim was signed.")
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description="sr25519 signature over the canonical top-5 claim.",
        ),
    ]

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must include a timezone")
        return value


class ConfirmationDatasetPin(BaseModel):
    """One platform-generated dataset used by a continual confirmation lease."""

    seed: Annotated[int, Field(ge=0)]
    dataset_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    run_size: Annotated[str, Field(min_length=1)]


class JobResponse(BaseModel):
    """Returned by ``POST /validator/job`` when a ticket is issued.

    A ticket grants this validator the right to score one agent by ``deadline``;
    the platform issues at most three per agent (the k=3 pool) and answers **204**
    (no body) when there is no work. ``seed`` + ``dataset_sha256`` identify the
    exact platform-pinned dataset all k=3 validators score (the scoring engine
    regenerates it from ``seed`` and rejects a hash mismatch), and ``run_size`` is
    the generator profile to use. These are null only for agents promoted before
    the data-pipeline split, or when platform-side generation is disabled.
    """

    agent_id: Annotated[UUID, Field(description="Agent this ticket is for.")]
    slot_id: Annotated[str, Field(pattern=r"^slot-[0-7]$")] = "slot-0"
    miner_hotkey: Annotated[str, Field(description="Submitting miner's SS58 hotkey.")]
    sha256: Annotated[
        str, Field(description="SHA-256 of the uploaded tarball, lowercase hex.")
    ]
    deadline: Annotated[
        datetime,
        Field(description="Score before this (UTC) or the ticket lapses."),
    ]
    seed: Annotated[
        int | None,
        Field(default=None, description="Platform-pinned dataset seed (regenerable)."),
    ] = None
    seed_scope: Literal["agent", "validator"] = Field(
        default="agent",
        description="Inputs used by the on-chain seed derivation. Validator "
        "scope additionally binds the ticket holder's hotkey.",
    )
    dataset_sha256: Annotated[
        str | None,
        Field(default=None, description="SHA-256 of the pinned dataset (tamper pin)."),
    ] = None
    run_size: Annotated[
        str | None,
        Field(default=None, description="Generator profile (small|medium|full)."),
    ] = None
    bench_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description="Version-bound benchmark semantics for this lease.",
        ),
    ] = None
    minimum_screening_policy_version: Annotated[
        int | None, Field(default=None, ge=0)
    ] = None
    requires_screened_image: bool | None = None
    confirmation_datasets: list[ConfirmationDatasetPin] = Field(
        default_factory=list,
        description="Exact shared-seed datasets pinned for a continual retest lease.",
    )
    inference: InferenceGrantOffer | None = None
    dataset_seed_block: Annotated[
        int | None,
        Field(
            default=None,
            description="Chain block number the dataset seed derives from.",
        ),
    ] = None
    dataset_seed_block_hash: Annotated[
        str | None,
        Field(
            default=None,
            description="Hash of ``dataset_seed_block``; lets the validator "
            "re-derive the seed itself and refuse a ground ticket "
            "(ditto/validator/onchain_seed.py).",
        ),
    ] = None


class FailJobRequest(BaseModel):
    """Fresh, one-time signed request to hand a leased ticket back on failure.

    A validator whose scoring attempt failed (its own infrastructure, or an
    ordinary bench/platform error) posts this so the platform closes the live
    lease immediately instead of leaving it to expire, and the next
    ``POST /validator/job`` issues a FRESH ticket rather than resuming the failed
    one. The signature proves possession of ``validator_hotkey`` and binds the
    exact ticket identity (``agent_id`` + ``ticket_deadline``); ``nonce`` is
    consumed once and ``requested_at`` is freshness-bounded.
    """

    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Reporting validator hotkey.")
    ]
    agent_id: Annotated[UUID, Field(description="Agent whose ticket failed.")]
    ticket_deadline: Annotated[
        datetime,
        Field(description="Deadline of the live lease being handed back (UTC)."),
    ]
    reason: Annotated[
        FailJobReason,
        Field(description="Coarse, source-free failure classification."),
    ]
    nonce: Annotated[UUID, Field(description="One-time report nonce.")]
    requested_at: Annotated[
        datetime, Field(description="UTC time at which the report was signed.")
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description="sr25519 signature over the canonical fail payload.",
        ),
    ]

    @field_validator("ticket_deadline", "requested_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value


class FailJobResponse(BaseModel):
    """Returned by ``POST /validator/job/fail`` when a lease is handed back.

    ``reopened`` is ``True`` when the caller held the live ticket and the
    platform closed it for immediate reissue; ``False`` when there was no live
    lease to close (already expired / scored), so the report was a harmless
    no-op.
    """

    agent_id: Annotated[UUID, Field(description="Echoes the reported agent id.")]
    reopened: Annotated[
        bool,
        Field(description="True when a live lease was closed for immediate reissue."),
    ]


class ValidatorHeartbeatRequest(BaseModel):
    """Signed proof of the validator build and its current runtime state."""

    model_config = ConfigDict(extra="forbid")

    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Reporting validator hotkey.")
    ]
    software_version: Annotated[
        str,
        Field(
            pattern=_SOFTWARE_VERSION_PATTERN,
            description="Ditto package version.",
        ),
    ]
    protocol_version: Annotated[
        int, Field(ge=1, le=2**31 - 1, description="Heartbeat protocol version.")
    ]
    code_digest: Annotated[
        str,
        Field(
            pattern=_CODE_DIGEST_PATTERN,
            description="SHA-256 of the installed validator Python source.",
        ),
    ]
    state: Annotated[
        ValidatorRuntimeState,
        Field(description="Current validator worker phase."),
    ]
    active_agent_id: Annotated[
        UUID | None,
        Field(
            default=None,
            description="Agent currently being benchmarked under protocol v2.",
        ),
    ] = None
    system_metrics: Annotated[
        SystemMetrics | None,
        Field(
            default=None,
            description="Optional coarse host telemetry under heartbeat protocol v3.",
        ),
    ] = None
    benchmark_progress: Annotated[
        BenchmarkProgress | None,
        Field(
            default=None,
            description="Optional privacy-safe benchmark progress under protocol v4.",
        ),
    ] = None
    capabilities: Annotated[
        ValidatorCapabilities | None,
        Field(default=None, description="Signed execution capabilities under v7."),
    ] = None
    stack: Annotated[
        ValidatorStackIdentity | None,
        Field(
            default=None, description="Signed six-component stack identity under v7."
        ),
    ] = None
    stack_health: Annotated[
        ValidatorStackHealth | None,
        Field(
            default=None,
            description="Signed per-component runtime health under v9.",
        ),
    ] = None
    benchmark_capacity: Annotated[
        BenchmarkCapacity | None,
        Field(
            default=None,
            description=(
                "Signed bounded slot capacity and progress under protocol v10+."
            ),
        ),
    ] = None
    timestamp: Annotated[
        int, Field(ge=0, description="Validator-reported Unix timestamp (UTC).")
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description="sr25519 signature over the canonical heartbeat payload.",
        ),
    ]

    @model_validator(mode="after")
    def progress_requires_v4_active_ticket(self) -> ValidatorHeartbeatRequest:
        if self.benchmark_progress is None:
            return self
        if self.protocol_version < 4:
            raise ValueError("benchmark progress requires heartbeat protocol v4")
        if self.state != "running_benchmark" or self.active_agent_id is None:
            raise ValueError(
                "benchmark progress requires running_benchmark and active_agent_id"
            )
        return self

    @model_validator(mode="after")
    def capabilities_require_v7(self) -> ValidatorHeartbeatRequest:
        identity_present = self.capabilities is not None or self.stack is not None
        if self.protocol_version >= 7:
            if self.capabilities is None or self.stack is None:
                raise ValueError(
                    "heartbeat protocol v7 requires capabilities and stack"
                )
            if self.capabilities.full_stack_managed != (self.stack.mode == "managed"):
                raise ValueError("managed capability must match stack mode")
        elif identity_present:
            raise ValueError("capabilities and stack require heartbeat protocol v7")
        scorer_capability = (
            self.capabilities.scorer_benchmarks
            if self.capabilities is not None
            else None
        )
        if self.protocol_version >= 8 and scorer_capability is None:
            raise ValueError("heartbeat protocol v8 requires scorer capability")
        if scorer_capability is not None and self.protocol_version < 8:
            raise ValueError(
                "scorer benchmark capability requires heartbeat protocol v8"
            )
        return self

    @model_validator(mode="after")
    def stack_health_requires_v9(self) -> ValidatorHeartbeatRequest:
        if self.protocol_version >= 9 and self.stack_health is None:
            raise ValueError("heartbeat protocol v9 requires stack health")
        if self.stack_health is not None and self.protocol_version < 9:
            raise ValueError(
                "per-component stack health requires heartbeat protocol v9"
            )
        if self.protocol_version >= 10:
            if self.benchmark_capacity is None:
                raise ValueError("heartbeat protocol v10+ requires benchmark capacity")
            primary = (
                sorted(self.benchmark_capacity.active, key=lambda slot: slot.slot_id)[0]
                if self.benchmark_capacity.active
                else None
            )
            if primary is None:
                if (
                    self.active_agent_id is not None
                    or self.benchmark_progress is not None
                ):
                    raise ValueError(
                        "idle v10+ capacity cannot carry legacy active work"
                    )
            elif (
                self.state != "running_benchmark"
                or self.active_agent_id != primary.agent_id
                or self.benchmark_progress != primary.progress
            ):
                raise ValueError(
                    "v10+ legacy active fields must mirror the first active slot"
                )
        elif self.benchmark_capacity is not None:
            raise ValueError("benchmark capacity requires heartbeat protocol v10+")
        return self


class ValidatorHeartbeatResponse(BaseModel):
    """Acknowledgement that a signed heartbeat was persisted."""

    accepted: bool
    seen_at: datetime


class ArtifactResponse(BaseModel):
    """Returned by ``GET /validator/agent/{agent_id}/artifact``.

    ``download_url`` is a short-lived pre-signed object-store URL the
    daemon GETs to stream the tarball. ``sha256`` lets the daemon verify
    the bytes it pulls against what the miner registered.
    """

    agent_id: Annotated[UUID, Field(description="Echoes the path-param id.")]
    sha256: Annotated[
        str, Field(description="Expected SHA-256 of the tarball, lowercase hex.")
    ]
    download_url: Annotated[
        str, Field(description="Pre-signed URL to GET the tarball bytes.")
    ]
    expires_at: Annotated[
        datetime, Field(description="When ``download_url`` stops being valid (UTC).")
    ]
    bench_version: Annotated[int | None, Field(default=None, ge=1)] = None
    screening_policy_version: Annotated[int | None, Field(default=None, ge=0)] = None
    screened_image_url: Annotated[
        str | None,
        Field(
            min_length=1,
            description="Pre-signed Docker image archive URL when screening built one.",
        ),
    ] = None
    screened_image_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = (
        None
    )
    screened_image_size_bytes: Annotated[int | None, Field(gt=0)] = None
    screened_image_id: Annotated[
        str | None, Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ] = None
    screened_image_ref: Annotated[str | None, Field(min_length=1)] = None

    @model_validator(mode="after")
    def screened_image_fields_are_atomic(self) -> ArtifactResponse:
        fields = (
            self.screened_image_url,
            self.screened_image_sha256,
            self.screened_image_size_bytes,
            self.screened_image_id,
            self.screened_image_ref,
        )
        if any(value is not None for value in fields) and any(
            value is None for value in fields
        ):
            raise ValueError("screened image metadata must be complete")
        if (
            self.bench_version is not None
            and self.bench_version >= 3
            and (
                self.screening_policy_version is None
                or self.screening_policy_version < 9
                or any(value is None for value in fields)
            )
        ):
            raise ValueError(
                f"benchmark v{self.bench_version} requires a policy-9 verified "
                "screened image"
            )
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "sha256": "deadbeef" * 8,
                "download_url": (
                    "https://minio.local/ditto-agents/"
                    "550e8400-e29b-41d4-a716-446655440000.tar.gz?X-Amz-..."
                ),
                "expires_at": "2026-06-08T12:05:00Z",
            }
        }
    )


class CaseScore(BaseModel):
    """Per-case breakdown inside a :class:`ScoreReport`.

    Mirrors the DittoBench ``CaseScore`` wire shape (``pkg/protocol``). Optional
    on the submission path; daemons may post only the aggregate. Scoring is
    judge-free and deterministic: a *tool* case carries ``tool_score``
    (deterministic trajectory + argument accuracy) and a *memory* case carries
    its per-``answer_kind`` result in ``score``. ``kind`` discriminates the two
    (empty on the tool-only practice path). ``quality`` and ``correct`` are
    legacy fields, unused under judge-free scoring.
    """

    case_id: Annotated[str, Field(description="Stable id of the scored case.")]
    category: Annotated[str, Field(description="Case category, e.g. ``web_search``.")]
    kind: Annotated[
        str, Field(default="", description="``tool`` | ``memory`` (empty if unset).")
    ]
    score: Annotated[
        float, Field(ge=0.0, le=1.0, description="Per-case composite in [0,1].")
    ]
    tool_score: Annotated[
        float, Field(ge=0.0, le=1.0, description="Per-case tool accuracy in [0,1].")
    ]
    quality: Annotated[
        float,
        Field(ge=0.0, le=1.0, default=0.0, description="LLM tool-quality judge [0,1]."),
    ]
    correct: Annotated[
        bool, Field(default=False, description="Memory judge verdict (memory cases).")
    ]
    latency_ms: Annotated[
        int, Field(ge=0, description="Observed latency for the case.")
    ]
    called: Annotated[
        list[str],
        Field(default_factory=list, description="Tool names the agent called."),
    ]
    expected: Annotated[
        list[str],
        Field(default_factory=list, description="Tool names the case expected."),
    ]
    notes: Annotated[
        list[str], Field(default_factory=list, description="Scorer annotations.")
    ]
    # bench_version 3 audit fields. Declared so ingest retains them — pydantic's
    # default ``extra="ignore"`` silently discarded them before, stripping audit
    # context (v3 review finding 16). None affects the composite; they mirror
    # ``dittobench-datagen/protocol`` ``CaseScore`` and the platform copy
    # (guarded by the wire round-trip test + the validator contract golden).
    result_usage: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            default=0.0,
            description=(
                "Result-usage half of an observed tool case: did the final "
                "answer incorporate the value only the executed tool served."
            ),
        ),
    ] = 0.0
    twin_group: Annotated[
        str,
        Field(
            default="",
            description=(
                "Metamorphic twin-group id tying rephrasings of one fact, for "
                "consistency audits."
            ),
        ),
    ] = ""
    confidence: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "Harness self-reported confidence echoed for Brier calibration "
                "(None = not reported; distinct from 0.0)."
            ),
        ),
    ] = None
    observed: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True when the graded trajectory is the validator-observed one "
                "(mock tool endpoint), i.e. ``called`` is authoritative."
            ),
        ),
    ] = False
    injection: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True when the grader flagged injection compliance on this case."
            ),
        ),
    ] = False

    @field_validator("called", "expected", "notes", mode="before")
    @classmethod
    def _none_to_empty(cls, v: list[str] | None) -> list[str]:
        # The Go scorer omits or nulls these on cases that have none (memory
        # cases carry no expected tools); coerce null/absent to an empty list.
        return v if v is not None else []


class CategoryStat(BaseModel):
    """Per-category aggregate inside a :class:`ScoreReport`.

    Mirrors the DittoBench ``CategoryStat`` wire shape (``pkg/protocol``).
    Advisory audit context only; the composite never depends on it.
    """

    category: Annotated[str, Field(description="Case category, e.g. ``web_search``.")]
    count: Annotated[int, Field(ge=0, description="Cases scored in the category.")]
    mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean case score in [0,1].")
    ]
    std_err: Annotated[
        float,
        Field(
            ge=0.0,
            default=0.0,
            description="Standard error of the category mean (0 when omitted).",
        ),
    ] = 0.0


class CodeFingerprint(BaseModel):
    """A bottom-k MinHash (KMV) sketch of a submission's source.

    Mirrors the DittoBench ``CodeFingerprint`` wire shape (``pkg/protocol``) and is
    byte-compatible with the platform's own fingerprint sketch, so the anti-copy
    gate compares them with one code path. Advisory moderation metadata only —
    never part of the score, and deliberately *not* covered by the report signature
    (see :class:`SubmitScoreRequest`). ``v`` is the sketch-format version, ``k`` the
    bottom-k budget, ``card`` the true shingle-set cardinality, and ``m`` the sorted
    bottom-``k`` shingle hashes.
    """

    v: Annotated[int, Field(ge=0, description="Sketch-format version.")]
    k: Annotated[int, Field(ge=1, description="Bottom-k sketch budget.")]
    card: Annotated[int, Field(ge=0, description="True shingle-set cardinality.")]
    m: Annotated[
        list[str], Field(default_factory=list, description="Sorted bottom-k hashes.")
    ]


class ScoreReport(BaseModel):
    """A completed DittoBench evaluation result for one agent.

    Mirrors the Go validator's ``ScoreReport`` so the scoring engine's
    output round-trips through ``POST /validator/agent/{id}/score``
    unchanged. ``composite = 0.5*tool_mean + 0.5*memory_mean`` (before the
    gate factors) when both kinds are present; the platform does not
    recompute it, it records what the daemon reports.
    """

    run_id: Annotated[str, Field(description="Scoring-engine run identifier.")]
    bench_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Version bound into new score signatures. Omission is accepted "
                "only for legacy benchmark-v2 leases."
            ),
        ),
    ] = None
    seed: Annotated[
        int,
        Field(
            ge=-(2**63),
            le=2**63 - 1,
            description="Dataset seed used (anti-overfit reproducibility); "
            "bounded to the signed 64-bit range the ``scores.seed`` column stores.",
        ),
    ]
    composite: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Aggregate score after any bounded waste penalty, in [0,1].",
        ),
    ]
    raw_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description="Pre-efficiency quality composite for benchmark v5.",
        ),
    ] = None
    tool_mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean tool accuracy in [0,1].")
    ]
    memory_mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean memory recall in [0,1].")
    ]
    median_ms: Annotated[int, Field(ge=0, description="Median per-case latency (ms).")]
    n: Annotated[int, Field(ge=0, description="Number of cases scored.")]
    composite_stderr: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            description=(
                "Optional standard error of the composite for this run. Surfaced "
                "on the scoring ledger so the validator's KOTH fold can gate a "
                "challenger on measurement uncertainty (the indifference band) "
                "instead of a flat margin. Additive-optional; not covered by the "
                "signature and never affects the score. Declared here so the "
                "engine's reported value survives parsing and reaches the "
                "platform (pydantic drops undeclared keys)."
            ),
        ),
    ] = None
    confirmation_composites: Annotated[
        list[float] | None,
        Field(
            default=None,
            description=(
                "Per-seed composites for a version-bump re-score (prod hardening "
                "P4). When the validator re-scores a stale champion/tail agent on "
                "K common CRN seeds it submits ONE score (this report, for the "
                "median-composite run) and lists all K per-seed composites here so "
                "the KOTH fold can gate a dethrone on the MEDIAN over seeds. "
                "Advisory: not covered by the signature and never affects the "
                "score. Null for a normal single-seed run."
            ),
        ),
    ] = None
    confirmation_seeds: Annotated[
        list[int] | None,
        Field(
            default=None,
            description=(
                "The K common CRN seeds aligned 1:1 (same order) with "
                "``confirmation_composites`` for a version-bump re-score, so the "
                "KOTH fold can PAIR a challenger against the champion on their "
                "shared seeds and use the lower paired-difference variance for the "
                "dethrone band. Advisory: not covered by the signature and never "
                "affects the score. Null for a normal single-seed run."
            ),
        ),
    ] = None
    generated_at: Annotated[
        datetime, Field(description="When the report was produced (UTC).")
    ]
    per_case: Annotated[
        list[CaseScore],
        Field(default_factory=list, description="Optional per-case breakdown."),
    ]
    per_category: Annotated[
        list[CategoryStat] | None,
        Field(
            default=None,
            description=(
                "Optional per-category aggregates (bench_version 3 audit "
                "context). Advisory: not covered by the signature and never "
                "affects the score."
            ),
        ),
    ] = None
    structural_fingerprint: Annotated[
        CodeFingerprint | None,
        Field(
            default=None,
            description=(
                "Optional AST-level structural sketch of the crate, computed by the "
                "scoring engine. Advisory anti-copy metadata; not covered by the "
                "signature and never affects the score. Null on the local "
                "harness_url path or when the crate has no parseable Rust."
            ),
        ),
    ]
    details: Annotated[
        dict[str, Any] | None,
        Field(
            default=None,
            description=(
                "Optional opaque run telemetry from the scoring engine — the "
                "models used, bench_version, dataset_sha256, per-category means, "
                "paraphrase / lexical-gap stats, and token spend. Advisory only: "
                "not covered by the signature and never affects the score. "
                "Persisted verbatim to scores.details for the transparency "
                "leaderboard."
            ),
        ),
    ]


class SubmitScoreRequest(BaseModel):
    """Body of ``POST /validator/agent/{agent_id}/score``.

    The validator authenticates by signing a canonical payload binding the
    agent id, exact ticket lease, and report contents. The platform reconstructs
    and verifies the same bytes, so a captured response from an expired lease
    cannot be replayed after the ticket is reissued.
    """

    validator_hotkey: Annotated[
        str,
        Field(pattern=_SS58_PATTERN, description="Reporting validator's SS58 hotkey."),
    ]
    ticket_deadline: Annotated[
        datetime | None,
        Field(
            default=None,
            description="Exact deadline from the JobResponse ticket lease.",
        ),
    ] = None
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description=(
                "Hex sr25519 signature over "
                "the agent, exact ticket deadline, run, composite, and seed."
            ),
        ),
    ]
    report: Annotated[ScoreReport, Field(description="The DittoBench score report.")]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "validator_hotkey": (
                    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
                ),
                "ticket_deadline": "2026-07-09T12:30:00Z",
                "signature": "ab" * 64,
                "report": {
                    "run_id": "run_2026-06-08_abc123",
                    "seed": 8675309,
                    "composite": 0.82,
                    "tool_mean": 0.88,
                    "memory_mean": 0.73,
                    "median_ms": 812,
                    "n": 30,
                    "generated_at": "2026-06-08T12:04:30Z",
                    "per_case": [],
                },
            }
        }
    )


class ConfirmationScoreRecord(BaseModel):
    """One append-only shared-seed confirmation score for a top-5 agent."""

    seed: Annotated[int, Field(ge=0, description="Champion-anchored CRN seed.")]
    composite: Annotated[
        float, Field(ge=0.0, le=1.0, description="Composite scored on this seed.")
    ]
    validator_hotkey: Annotated[
        str, Field(description="SS58 hotkey of the validator that scored this seed.")
    ]
    bench_version: Annotated[
        int,
        Field(ge=1, description="Major bench version the seed family is scoped to."),
    ]
    signature: Annotated[
        str | None,
        Field(
            default=None, description="Validator's hex sr25519 signature, if stored."
        ),
    ] = None


class LedgerScoreProof(BaseModel):
    """One validator's complete, independently verifiable score receipt."""

    validator_hotkey: Annotated[str, Field(description="Signing validator hotkey.")]
    run_id: Annotated[str, Field(description="Signed benchmark run id.")]
    composite: Annotated[float, Field(ge=0.0, le=1.0)]
    seed: Annotated[int, Field(description="Signed dataset seed.")]
    bench_version: Annotated[int | None, Field(default=None, ge=1)] = None
    ticket_deadline: Annotated[
        datetime | None,
        Field(default=None, description="Exact signed lease deadline."),
    ] = None
    transcript_sha256: Annotated[
        str | None,
        Field(default=None, description="Signed transcript digest, when present."),
    ] = None
    signature: Annotated[
        str | None,
        Field(default=None, description="Hex sr25519 signature over this receipt."),
    ] = None


class LedgerEntry(BaseModel):
    """One miner's best eligible score, returned by ``GET /scoring/scores``.

    The public score pool the validator folds into KOTH+ATH
    weights. One entry per active miner = that miner's highest-scoring eligible
    agent (status ``scored``). ``first_seen`` (the agent's upload time) is the
    tie-break that lets the original beat a later copy of the same score;
    ``composite`` is the raw reported double (never rounded, so every validator
    folds identical bytes). ``signature`` is the reporting validator's sr25519
    signature so the ledger is self-verifying.
    """

    miner_hotkey: Annotated[str, Field(description="Miner's SS58 hotkey.")]
    agent_id: Annotated[UUID, Field(description="The miner's best eligible agent.")]
    composite: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="Best aggregate benchmark score in [0,1].",
        ),
    ]
    n: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Cases scored in the winning run. The validator's eligibility "
                "floor (MIN_ELIGIBLE_CASES): a run below it is a smoke/practice "
                "profile and is dropped from the weight fold, so it can never rank."
            ),
        ),
    ]
    first_seen: Annotated[
        datetime,
        Field(description="Agent upload time (UTC); the KOTH first-seen tie-break."),
    ]
    sha256: Annotated[str, Field(description="SHA-256 of the tarball, lowercase hex.")]
    size_bytes: Annotated[
        int | None, Field(default=None, ge=0, description="Tarball size in bytes.")
    ]
    run_id: Annotated[
        str,
        Field(description="Run id of the scoring run (part of the signed payload)."),
    ]
    seed: Annotated[int, Field(description="Dataset seed of the scoring run.")]
    validator_hotkey: Annotated[
        str, Field(description="SS58 hotkey of the validator that produced the score.")
    ]
    bench_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Benchmark contract version of this platform-authoritative row. "
                "During a rollout the ledger can intentionally mix versions: an "
                "agent moves to the desired version only after quorum."
            ),
        ),
    ] = None
    signature: Annotated[
        str | None,
        Field(
            default=None,
            description="Validator's hex sr25519 signature, if stored.",
        ),
    ]
    score_proofs: Annotated[
        list[LedgerScoreProof],
        Field(
            default_factory=list,
            description=(
                "All signed validator receipts in the authoritative quorum. "
                "Validators verify these and recompute the median before weights."
            ),
        ),
    ]
    composite_stderr: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            description=(
                "Standard error of the composite for the winning run, if the "
                "platform surfaced one. Feeds the KOTH fold's measurement-"
                "uncertainty indifference band (weights.py ``_beats``). "
                "Additive-optional: absent means the fold uses the flat relative "
                "margin. Declared here so the wire value survives parsing "
                "(pydantic drops undeclared keys)."
            ),
        ),
    ] = None
    confirmation_composites: Annotated[
        list[float] | None,
        Field(
            default=None,
            description=(
                "Per-seed composites for this agent from a version-bump re-score "
                "over K common CRN seeds (prod hardening P4), when the platform "
                "surfaces them. With two or more values the KOTH dethrone "
                "comparison uses their MEDIAN instead of the single-run composite, "
                "so a crown flip must replicate across seeds. Additive-optional: "
                "absent means the fold uses the raw composite."
            ),
        ),
    ] = None
    confirmation_seeds: Annotated[
        list[int] | None,
        Field(
            default=None,
            description=(
                "The K common CRN seeds aligned 1:1 with "
                "``confirmation_composites`` for this agent's version-bump "
                "re-score, when the platform surfaces them. Lets the KOTH fold "
                "pair a challenger against the champion on shared seeds (lower "
                "paired-difference variance) instead of the independent-sum band. "
                "Additive-optional: absent means the fold uses the unpaired band."
            ),
        ),
    ] = None
    confirmation_history: Annotated[
        list[ConfirmationScoreRecord] | None,
        Field(
            default=None,
            description=(
                "Append-only shared-seed confirmation scores for this agent from "
                "the continual top-5 rescore lane (ditto-platform #280), one row "
                "per ``(validator_hotkey, bench_version, seed)`` — immutable and "
                "accumulating over the agent's reign. Supersedes the in-row "
                "``confirmation_composites``/``confirmation_seeds`` arrays as the "
                "fold's paired-evidence source: the KOTH fold groups these by "
                "seed. Additive-optional: absent means the fold falls back to the "
                "legacy in-row arrays (then the unpaired band)."
            ),
        ),
    ] = None
    continual_aggregate_method: Literal["mean_after_quorum"] | None = Field(
        default=None,
        description=(
            "Platform activation marker for protocol v14+. When present, the "
            "weight fold uses the arithmetic mean of the three signed quorum "
            "scores plus one aggregate per completed continual cohort wave."
        ),
    )
    status: Annotated[
        AgentStatus, Field(description="Agent lifecycle state (always ``scored``).")
    ]


class LedgerResponse(BaseModel):
    """Returned by ``GET /scoring/scores``.

    ``entries`` is ordered highest-composite first (ties broken by ``first_seen``
    then ``agent_id``), the same deterministic order the validator's fold uses,
    so the exposed pool and the computed weights agree by construction.
    """

    entries: Annotated[
        list[LedgerEntry],
        Field(
            description=(
                "Best eligible score per payment-time coldkey, highest composite "
                "first; each row's miner hotkey is the selected weight destination."
            )
        ),
    ]
    count: Annotated[int, Field(ge=0, description="Number of entries returned.")]
    generated_at: Annotated[
        datetime | None,
        Field(default=None, description="When the ledger was read from the DB (UTC)."),
    ] = None
    stale: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True when the platform served a last-known-good snapshot because "
                "its live DB read failed. The fold may still use it (the ledger is "
                "durable and slow-moving) but should log it as advisory."
            ),
        ),
    ] = False
    age_seconds: Annotated[
        int,
        Field(default=0, ge=0, description="Age of the served snapshot in seconds."),
    ] = 0

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "entries": [
                    {
                        "miner_hotkey": (
                            "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
                        ),
                        "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                        "composite": 0.82,
                        "n": 114,
                        "first_seen": "2026-06-08T12:00:00Z",
                        "sha256": "deadbeef" * 8,
                        "size_bytes": 524288,
                        "run_id": "run_2026-06-08_abc123",
                        "seed": 8675309,
                        "validator_hotkey": (
                            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
                        ),
                        "signature": "ab" * 64,
                        "status": "scored",
                    }
                ],
                "count": 1,
            }
        }
    )


class SubmitScoreResponse(BaseModel):
    """Returned by ``POST /validator/agent/{agent_id}/score``.

    ``status`` is the agent's lifecycle state *after* recording the score
    (``scored``). ``accepted`` is ``True`` when the report was persisted;
    it leaves room for a future soft-reject (e.g. duplicate report for the
    same run) without changing the status code.
    """

    agent_id: Annotated[UUID, Field(description="Echoes the path-param id.")]
    status: Annotated[
        AgentStatus, Field(description="Lifecycle state after recording the score.")
    ]
    accepted: Annotated[
        bool, Field(description="``True`` when the report was recorded.")
    ]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "scored",
                "accepted": True,
            }
        }
    )
