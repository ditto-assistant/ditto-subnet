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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.benchmark_progress import BenchmarkProgress
from ditto.api_models.confirmation_bundles import (
    AblationDimensionEnvelope as AblationDimensionEnvelope,
)
from ditto.api_models.confirmation_bundles import (
    ConfirmationUsageTotals as ConfirmationUsageTotals,
)
from ditto.api_models.confirmation_bundles import (
    LongMemDimensionEnvelope as LongMemDimensionEnvelope,
)
from ditto.api_models.confirmation_progress import (
    MAX_CONFIRMATION_SLOTS,
    ConfirmationProgress,
)
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
from ditto.api_models.validator_updater import ValidatorUpdaterStatus
from ditto_screening_protocol.bench_v9 import (
    V9AuthoritativeToolGate as V9AuthoritativeToolGate,
)
from ditto_screening_protocol.bench_v9 import (
    V9BaseEvidence as V9BaseEvidence,
)
from ditto_screening_protocol.bench_v9 import (
    V9GateExclusions as V9GateExclusions,
)
from ditto_screening_protocol.bench_v9 import (
    V9GateResult as V9GateResult,
)
from ditto_screening_protocol.bench_v9 import (
    V9ModelUseGate as V9ModelUseGate,
)
from ditto_screening_protocol.bench_v9 import (
    V9ScoreContract as V9ScoreContract,
)
from ditto_screening_protocol.bench_v9 import (
    V9ScoreGateEvidence as V9ScoreGateEvidence,
)
from ditto_screening_protocol.bench_v9 import (
    V9ThresholdProfile as V9ThresholdProfile,
)
from ditto_screening_protocol.bench_v9 import normalize_v9_score_report_omitempty
from ditto_screening_protocol.confirmation import (
    V9ConfirmationCompositePolicy as V9ConfirmationCompositePolicy,
)
from ditto_screening_protocol.confirmation import (
    V9ConfirmationEvidenceRoot,
)

_CODE_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_SOFTWARE_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
ValidatorRuntimeState = Literal[
    "polling",
    "running_benchmark",
    "updating_weights",
    "idle",
    "error",
    "paused",
]


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
    bench_version: Annotated[int | None, Field(default=None, ge=1)] = None
    screening_policy_version: Annotated[int | None, Field(default=None, ge=0)] = None

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


class JobRequest(BaseModel):
    """Signed request to claim one validator scoring ticket.

    The signature proves possession of ``validator_hotkey`` before the platform
    allocates scarce quorum work. ``nonce`` is consumed exactly once and
    ``requested_at`` is freshness-bounded, preventing a captured request from
    claiming another ticket later.
    """

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
    """Fresh signed claim for a member of the top-5 shared-seed rescore lane.

    Covers the whole emission set (champion + participation tail). The
    validator claims one ticket per set member it wants to rescore this round,
    each anchored to the current champion so the platform can rebuild the same
    emission set and validate that ``member_agent_id`` is either the champion or
    a current tail entrant, and that ``champion_agent_id`` is the reigning
    incumbent (the CRN seed anchor both sides derive identically).
    """

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
    the platform issues at most three per agent (the k=3 pool) and answers 204
    (no body) when there is no work. The validator fetches the tarball via
    ``/artifact`` and scores it against the platform-pinned dataset: ``seed`` +
    ``dataset_sha256`` identify the exact dataset for this validator's quorum
    run (the scoring API regenerates it and rejects a hash mismatch), and
    ``run_size`` is the generator profile to use. These are null only for agents
    promoted before the data-pipeline split, or when generation is disabled.
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
        Field(
            default=None,
            description="Post-commit block-derived dataset seed for this "
            "validator's quorum run. Distinct validator hotkeys receive distinct "
            "seeds; retries by the same validator retain the same seed.",
        ),
    ] = None
    seed_scope: Literal["agent", "validator"] = Field(
        default="agent",
        description="Inputs used by the on-chain seed derivation. Validator "
        "scope additionally binds the ticket holder's hotkey.",
    )
    dataset_sha256: Annotated[
        str | None,
        Field(
            default=None,
            description="SHA-256 of the pinned dataset; the scoring API fails if "
            "the regenerated dataset does not hash to this (tamper-evidence).",
        ),
    ] = None
    run_size: Annotated[
        str | None,
        Field(
            default=None,
            description="Generator profile for the pinned dataset (small|medium|full).",
        ),
    ] = None
    dataset_seed_block: Annotated[
        int | None,
        Field(
            default=None,
            description="Chain block number the dataset seed derives from "
            "(trustless verification; null for pre-derivation agents).",
        ),
    ] = None
    dataset_seed_block_hash: Annotated[
        str | None,
        Field(
            default=None,
            description="Hash of ``dataset_seed_block``. Lets the validator "
            "independently re-derive ``seed = derive_validator_seed(block_hash, "
            "agent_id, validator_hotkey)`` "
            "and refuse a ticket whose seed the platform could have chosen "
            "(anti-grind, prod hardening P2). Null for pre-derivation agents.",
        ),
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

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "miner_hotkey": ("5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"),
                "sha256": "deadbeef" * 8,
                "deadline": "2026-07-09T12:30:00Z",
                "seed": 8675309,
                "dataset_sha256": "cafebabe" * 8,
                "run_size": "full",
            }
        }
    )


FailJobReason = Literal[
    "infrastructure",
    "scoring_error",
    "sandbox_oom",
]
"""Coarse reason a validator hands a leased ticket back for reissue.

Deliberately low-cardinality so no run-specific detail leaks. The platform
branches on it: ``infrastructure`` earns a bounded compensating grant plus an
escalating cooldown, so a validator-side outage neither spends the agent's
genuine attempt budget nor hammers the failing provider; ``scoring_error`` is
the agent's own failure and consumes an attempt with an immediate reissue.
``sandbox_oom`` is an observed sandbox memory exhaustion: it consumes the
failed attempt, records a public-safe telemetry signal, and applies the normal
agent-failure cooldown so another eligible harness runs next. ``infrastructure``
maps to the validator's ``ValidatorInfrastructureError`` sweep-ending branch;
``scoring_error`` maps to other ``DittobenchError`` scoring failures. These
values are the wire contract shared verbatim with ditto-subnet (which emits
them).

Being low-cardinality is also what makes it useless for diagnosis, which is what
``FailJobRequest.failure_detail`` exists to carry.
"""

FAILURE_DETAIL_MAX_LENGTH = 4096
"""Cap on ``FailJobRequest.failure_detail``.

Was 200, which is the length of a failure code plus a short qualifier and
nothing more. That bound cost the very diagnosis the field was added for: on
2026-07-27 it cut

    ``... the platform rejected 81 of the harness's inference r``

mid-word, discarding ``equest(s) outright, before reserving any capacity`` --
the clause that named *what* the platform had done. The surviving half read as
a complete sentence, which is worse than an obvious stub. The count and the
verb survived only because of where they happened to fall in the sentence.

4096 is a deliberate ceiling rather than "unbounded". This field is written by
validators, on a hot table, once per failed ticket, and its content is derived
from strings a miner's harness can influence -- so an unbounded column is a
storage and log-volume liability with an adversarial input path into it. 4 KiB
is ~16x the longest real message observed and comfortably fits a scorer message
with a run id, a full account of an exhaustion, and its counts; it is still
small enough that the whole ledger cannot be turned into a log sink.

Overflow past this is still truncated by the sender, but no longer silently:
:func:`ditto.validator.errors.failure_detail` in ditto-subnet appends an
explicit ``...[truncated, N chars]`` marker, so a reader can tell an amputated
message from a whole one. Truncating on the sending side (rather than rejecting
here) remains the rule: a detail that overflows must never turn a hand-back into
a 422 and leave the lease to expire silently -- that would trade the diagnosis
this field adds for the ambiguity it exists to remove.

Public rather than underscore-private because ditto-subnet mirrors this number
by name and the golden contract in ``ditto/tests/contract`` pins it; the two
copies are only correct together.
"""

LEGACY_FAILURE_DETAIL_MAX_LENGTH = 200
"""The pre-widening cap, retained as documentation of the accepted floor.

Validators run mixed versions. Every one predating this change truncates to 200
before sending, and 200 <= :data:`FAILURE_DETAIL_MAX_LENGTH`, so those reports
keep validating unchanged -- widening a ``max_length`` can only ever admit more.
The reverse skew (a new validator against an old platform) is handled on the
sending side in ditto-subnet, which retries a 422'd hand-back once at this
bound.
"""


class FailJobRequest(BaseModel):
    """Signed request to hand a still-leased ticket back after a failed attempt.

    A validator whose scoring attempt failed calls this so the platform closes
    the live ticket immediately (status ``expired``, ``retry_after`` now) and the
    slot re-opens for a fresh ticket, instead of the lease sitting idle until its
    deadline. Mirrors :class:`JobRequest`'s proof-of-possession: the signature
    proves the hotkey, ``nonce`` is one-time, and ``requested_at`` is
    freshness-bounded. The ``(agent_id, ticket_deadline)`` pair identifies the
    exact lease the caller must currently hold.
    """

    validator_hotkey: Annotated[
        str, Field(pattern=_SS58_PATTERN, description="Failing validator hotkey.")
    ]
    agent_id: Annotated[UUID, Field(description="Agent whose ticket failed.")]
    ticket_deadline: Annotated[
        datetime,
        Field(description="Exact deadline from the JobResponse ticket lease."),
    ]
    reason: Annotated[
        FailJobReason,
        Field(
            description="Coarse failure class; drives the platform's reissue policy."
        ),
    ]
    failure_detail: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=FAILURE_DETAIL_MAX_LENGTH),
        Field(
            default=None,
            description=(
                "Reporter's own failure code or diagnostic message behind "
                "``reason``. Advisory: drives no policy, unsigned, and optional."
            ),
        ),
    ] = None
    nonce: Annotated[UUID, Field(description="One-time claim nonce.")]
    requested_at: Annotated[
        datetime, Field(description="UTC time at which the request was signed.")
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description="sr25519 signature over the canonical fail payload.",
        ),
    ]

    @field_validator("requested_at", "ticket_deadline")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return value


class FailJobResponse(BaseModel):
    """Returned by ``POST /validator/job/fail``.

    ``reopened`` is ``True`` when a live ticket matching the signed lease was
    found and closed for immediate reissue; ``False`` when the caller held no
    such live ticket (already expired, scored, or never issued) — a no-op that
    is safe to ignore, keeping the endpoint idempotent and best-effort.
    """

    agent_id: Annotated[UUID, Field(description="Echoes the failed agent id.")]
    reopened: Annotated[
        bool, Field(description="``True`` when a live ticket was closed for reissue.")
    ]


class ValidatorHeartbeatRequest(BaseModel):
    """Signed proof of the validator build currently serving a hotkey.

    ``code_digest`` is a deterministic SHA-256 over the installed Python source,
    so it identifies the actual worker bytes without trusting an operator-supplied
    Git label. ``timestamp`` is Unix time and is freshness-checked by the server.
    """

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
            description=(
                "Agent currently being benchmarked under heartbeat protocol v2."
            ),
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
            description=(
                "Optional ticket-bound benchmark progress under heartbeat protocol v4."
            ),
        ),
    ] = None
    capabilities: Annotated[
        ValidatorCapabilities | None,
        Field(default=None, description="Signed execution capabilities (protocol v7)."),
    ] = None
    stack: Annotated[
        ValidatorStackIdentity | None,
        Field(
            default=None, description="Signed complete stack identity (protocol v7)."
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
                "Signed bounded slot capacity and progress under protocols v10-v11."
            ),
        ),
    ] = None
    confirmation_progress: Annotated[
        list[ConfirmationProgress] | None,
        Field(
            default=None,
            max_length=MAX_CONFIRMATION_SLOTS,
            description=(
                "Signed independent LongMemEval/ablation slot progress under "
                "heartbeat protocol v22."
            ),
        ),
    ] = None
    updater_status: Annotated[
        ValidatorUpdaterStatus | None,
        Field(
            default=None,
            description="Signed sanitized managed-updater state under protocol v23.",
        ),
    ] = None
    timestamp: Annotated[
        int, Field(ge=0, description="Validator-reported Unix timestamp (UTC).")
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description=("sr25519 signature over the canonical v1 heartbeat payload."),
        ),
    ]

    @model_validator(mode="after")
    def validate_protocol_fields(self) -> ValidatorHeartbeatRequest:
        if self.protocol_version >= 7:
            if self.capabilities is None or self.stack is None:
                raise ValueError(
                    "heartbeat protocol v7 requires capabilities and stack"
                )
            if self.capabilities.full_stack_managed != (self.stack.mode == "managed"):
                raise ValueError("full_stack_managed contradicts stack mode")
            if (
                self.protocol_version == 7
                and self.capabilities.scorer_benchmarks is not None
            ):
                raise ValueError("scorer benchmark capability requires heartbeat v8")
            if (
                self.protocol_version >= 8
                and self.capabilities.scorer_benchmarks is None
            ):
                raise ValueError("heartbeat v8 requires scorer benchmark capability")
        elif self.capabilities is not None or self.stack is not None:
            raise ValueError("capabilities and stack require heartbeat protocol v7")
        if self.protocol_version >= 9 and self.stack_health is None:
            raise ValueError("heartbeat protocol v9 requires stack health")
        if self.stack_health is not None and self.protocol_version < 9:
            raise ValueError(
                "per-component stack health requires heartbeat protocol v9"
            )
        if self.protocol_version >= 10:
            if self.benchmark_capacity is None:
                raise ValueError("heartbeat protocol v10 requires benchmark capacity")
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
                        "idle v10 capacity cannot carry legacy active work"
                    )
            elif (
                self.state != "running_benchmark"
                or self.active_agent_id != primary.agent_id
                or self.benchmark_progress != primary.progress
            ):
                raise ValueError(
                    "v10 legacy active fields must mirror the first active slot"
                )
            if (
                self.protocol_version >= 11
                and self.capabilities is not None
                and self.capabilities.scorer_benchmarks is not None
                and 7 in self.capabilities.scorer_benchmarks.supported_bench_versions
                and (
                    not self.capabilities.ticket_inference
                    or self.capabilities.scorer_benchmarks.v7_calibration is None
                )
            ):
                raise ValueError(
                    "heartbeat v11 requires exact v7 inference calibration identity"
                )
            if self.protocol_version >= 22:
                if self.confirmation_progress is None:
                    raise ValueError(
                        "heartbeat protocol v22 requires confirmation progress"
                    )
                slots = [progress.slot_id for progress in self.confirmation_progress]
                if len(slots) != len(set(slots)):
                    raise ValueError("confirmation progress contains duplicate slots")
            elif self.confirmation_progress is not None:
                raise ValueError(
                    "confirmation progress requires heartbeat protocol v22"
                )
            if self.protocol_version >= 12 and (
                self.capabilities is None or not self.capabilities.signed_score_quorum
            ):
                raise ValueError(
                    "heartbeat v12 requires signed score quorum verification"
                )
            if (
                self.capabilities is not None
                and self.capabilities.signed_score_quorum
                and self.protocol_version < 12
            ):
                raise ValueError(
                    "signed score quorum verification requires heartbeat protocol v12"
                )
        elif self.benchmark_capacity is not None:
            raise ValueError("benchmark capacity requires heartbeat protocol v10")
        # v15 adds scorer liveness evidence. It is never *required*: telemetry
        # that can silence a validator is worse than telemetry that is missing,
        # and a v15 heartbeat that omits it already reads "unreported" in the
        # fleet view. What is refused is a validator claiming an older protocol
        # while sending a newer field, which would let the signed envelope and
        # the declared protocol disagree.
        if (
            self.protocol_version < 15
            and self.capabilities is not None
            and self.capabilities.scorer_benchmarks is not None
            and self.capabilities.scorer_benchmarks.probe is not None
        ):
            raise ValueError("scorer liveness probe requires heartbeat protocol v15")
        if self.protocol_version >= 23 and self.updater_status is None:
            raise ValueError("heartbeat protocol v23 requires updater status")
        if self.updater_status is not None and self.protocol_version < 23:
            raise ValueError("updater status requires heartbeat protocol v23")
        return self


class HeldLease(BaseModel):
    """One lease the platform's ledger says this validator holds right now.

    The ledger is the only authority on lease assignment, so this is what the
    validator should be executing — not what it believes it is executing.
    ``deadline`` is carried for logging and future use; it is deliberately *not*
    an identity term for the reporter's cancel decision, because a lease that is
    re-issued in place keeps its ``(slot_id, agent_id)`` while its deadline
    moves, and a deadline mismatch must never be able to authorize a kill.
    """

    model_config = ConfigDict(extra="forbid")

    slot_id: Annotated[
        str,
        Field(pattern=r"^slot-[0-7]$", description="Execution slot holding the lease."),
    ]
    agent_id: Annotated[UUID, Field(description="Agent this lease is scoped to.")]
    bench_version: Annotated[
        int, Field(ge=1, description="Benchmark version the lease was issued for.")
    ]
    deadline: Annotated[
        datetime, Field(description="UTC instant after which the lease lapses.")
    ]


class ValidatorHeartbeatResponse(BaseModel):
    """Acknowledgement that a signed heartbeat was persisted.

    ``leases`` is heartbeat protocol **17**: the platform's authoritative roster
    of the leases this validator currently holds. Its two absent-ish values are
    different states and must never be collapsed:

    * ``None`` — *the platform did not tell you.* The reporter declared a
      protocol below 17, the roster read failed, or the peer is a platform old
      enough not to have this field at all. Carries no information about any
      lease and can never justify stopping a run.
    * ``[]`` — *the platform told you that you hold nothing.* An authoritative,
      successfully-derived answer, and the one case where a reporter with a
      running benchmark should stop it.

    A reporter is expected to cancel a run only when the slot it advertised in
    the very request that produced this response is missing from a non-``None``
    roster. That request-then-read ordering is what makes the roster safe to act
    on without any clock comparison: the platform necessarily read its ledger
    after the reporter had already claimed the slot it sent.
    """

    accepted: bool
    seen_at: datetime
    leases: Annotated[
        list[HeldLease] | None,
        Field(
            default=None,
            description=(
                "Protocol v17 authoritative lease roster. ``null`` means "
                "'not answered' and is never grounds to cancel work; an empty "
                "list means 'you hold no lease'."
            ),
        ),
    ] = None


class CaseScore(BaseModel):
    """Per-case breakdown inside a :class:`ScoreReport`.

    Mirrors the DittoBench ``CaseScore`` wire shape (``pkg/protocol``). Optional
    on the submission path — daemons may post only the aggregate. Carries both
    case families: a *tool* case has ``tool_score`` (deterministic accuracy) +
    ``quality`` (LLM judge), with ``score = 0.5*tool_score + 0.5*quality``; a
    *memory* case has ``correct`` (LongMemEval yes/no) and ``score`` 1.0/0.0,
    with ``tool_score``/``quality`` unused. ``kind`` discriminates the two
    (empty on the tool-only practice path).
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
    # ``dittobench-datagen/protocol`` ``CaseScore`` and must stay in sync with
    # the ditto-subnet copy (guarded by the wire round-trip test).
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


class CodeFingerprint(BaseModel):
    """A bottom-k MinHash (KMV) sketch of a submission's source.

    Mirrors the DittoBench ``CodeFingerprint`` wire shape (``pkg/protocol``) and is
    byte-compatible with the platform's own fingerprint sketch
    (:mod:`ditto.api_server.fingerprint`), so the anti-copy gate compares them with
    one code path. Advisory moderation metadata only — never part of the score, and
    deliberately *not* covered by the report signature (see
    :class:`SubmitScoreRequest`). ``v`` is the sketch-format version, ``k`` the
    bottom-k budget, ``card`` the true shingle-set cardinality, and ``m`` the sorted
    bottom-``k`` shingle hashes.
    """

    v: Annotated[int, Field(ge=0, description="Sketch-format version.")]
    k: Annotated[int, Field(ge=1, description="Bottom-k sketch budget.")]
    card: Annotated[int, Field(ge=0, description="True shingle-set cardinality.")]
    m: Annotated[
        list[str], Field(default_factory=list, description="Sorted bottom-k hashes.")
    ]


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


class ScoreReport(BaseModel):
    """A completed DittoBench evaluation result for one agent.

    Mirrors the Go validator's ``ScoreReport`` so the scoring engine's
    output round-trips through ``POST /validator/agent/{id}/score``
    unchanged. ``composite = 0.6*tool_mean + 0.4*memory_mean`` when both
    kinds are present (the platform does not recompute it; it records
    what the daemon reports).
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
    base_evidence_sha256: Annotated[
        str | None,
        Field(
            default=None,
            pattern=_SHA256_PATTERN,
            exclude_if=lambda value: value is None,
            description=(
                "SHA-256 of the canonical typed details.v9_base root. Required "
                "exactly for benchmark v9 and covered by the score signature."
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
                "signature and never affects the score."
            ),
        ),
    ]
    confirmation_composites: Annotated[
        list[float] | None,
        Field(
            default=None,
            description=(
                "Per-seed composites for a version-bump re-score (prod hardening "
                "P4). When a validator re-scores a stale champion/tail agent on K "
                "common CRN seeds it submits one score (this report, the median "
                "run) and lists all K per-seed composites here so the KOTH fold "
                "dethrones on the median over seeds. Advisory: not covered by the "
                "signature and never affects the score. Stashed into "
                "``scores.details`` and surfaced on the ledger."
            ),
        ),
    ]
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
                "affects the score. Stashed into ``scores.details`` and surfaced "
                "on the ledger."
            ),
        ),
    ]
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

    @model_validator(mode="before")
    @classmethod
    def _normalize_v9_go_omitempty(cls, value: object) -> object:
        return normalize_v9_score_report_omitempty(value)

    @model_validator(mode="after")
    def _validate_v9_base_evidence(self) -> ScoreReport:
        details = self.details if isinstance(self.details, dict) else {}
        raw_evidence = details.get("v9_base")
        if self.bench_version != 9:
            if self.base_evidence_sha256 is not None or raw_evidence is not None:
                raise ValueError("v9 base evidence is only valid for benchmark v9")
            return self
        if self.base_evidence_sha256 is None or not isinstance(raw_evidence, dict):
            raise ValueError("benchmark v9 requires typed base evidence")
        evidence = V9BaseEvidence.model_validate(raw_evidence)
        if evidence.run_id != self.run_id:
            raise ValueError("v9 base evidence run_id does not match report")
        if evidence.dataset_sha256 != details.get("dataset_sha256"):
            raise ValueError("v9 base evidence dataset digest does not match report")
        if evidence.transcript_sha256 != details.get("transcript_sha256"):
            raise ValueError("v9 base evidence transcript digest does not match report")
        if self.composite != evidence.effective_composite_micros / 1_000_000:
            raise ValueError("v9 effective composite does not match report")
        if self.composite_stderr is None or self.composite_stderr != (
            evidence.effective_stderr_micros / 1_000_000
        ):
            raise ValueError("v9 effective stderr does not match report")
        if self.base_evidence_sha256 != evidence.digest_hex():
            raise ValueError("base_evidence_sha256 does not match details.v9_base")
        return self


class SubmitScoreRequest(BaseModel):
    """Body of ``POST /validator/agent/{agent_id}/score``.

    The validator authenticates by signing a canonical payload binding the
    agent id, exact ticket lease, and report contents — the UTF-8 bytes of
    ``f"{validator_hotkey}:{agent_id}:{ticket_deadline}:{run_id}:"`` +
    ``f"{composite!r}:{seed}"`` — with the validator's hotkey keypair. The
    platform reconstructs and verifies the same bytes, so a captured signature
    cannot be replayed against a different agent or a reissued ticket.
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
                "``{validator_hotkey}:{agent_id}:{ticket_deadline}:"
                "{run_id}:{composite!r}:{seed}``."
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
    """One append-only shared-seed confirmation score for a top-5 agent.

    Each row is one ``(validator_hotkey, bench_version, seed)`` confirmation the
    continual top-5 rescore lane produced for this agent (immutable,
    INSERT-idempotent on the platform's ``ConfirmationScore`` ledger — never
    updated). The KOTH fold groups these by ``seed`` to reconstruct the agent's
    per-seed composite map (paired lower-median), so a longer-reigning champion
    simply accumulates more rows and its band widens. Mirrors the shape of a
    signed score receipt (``seed`` + ``composite`` + the producing validator);
    the exact platform exposure is reconciled against ditto-platform #280.
    """

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
    """One validator-signed score receipt backing a ledger median."""

    validator_hotkey: Annotated[str, Field(description="Scoring validator hotkey.")]
    run_id: Annotated[str, Field(description="Signature-bound scoring run id.")]
    composite: Annotated[float, Field(ge=0.0, le=1.0)]
    seed: int
    bench_version: Annotated[int | None, Field(default=None, ge=1)] = None
    ticket_deadline: Annotated[
        datetime | None,
        Field(default=None, description="Signature-bound ticket lease deadline."),
    ] = None
    transcript_sha256: Annotated[
        str | None,
        Field(
            default=None,
            pattern=_SHA256_PATTERN,
            description="Signature-bound transcript digest.",
        ),
    ] = None
    base_evidence_sha256: Annotated[
        str | None,
        Field(
            default=None,
            pattern=_SHA256_PATTERN,
            exclude_if=lambda value: value is None,
            description="Signature-bound canonical v9 base-evidence digest.",
        ),
    ] = None
    base_evidence: Annotated[
        V9BaseEvidence | None,
        Field(
            default=None,
            exclude_if=lambda value: value is None,
            description="Typed v9 base root whose digest is signature-bound.",
        ),
    ] = None
    signature: Annotated[
        str | None,
        Field(default=None, description="Hex sr25519 signature for this receipt."),
    ] = None

    @model_validator(mode="after")
    def _validate_v9_evidence_identity(self) -> LedgerScoreProof:
        if self.bench_version == 9:
            if self.transcript_sha256 is None or self.base_evidence_sha256 is None:
                raise ValueError(
                    "benchmark v9 proof requires transcript and base evidence"
                )
            if self.base_evidence is not None:
                if self.base_evidence.digest_hex() != self.base_evidence_sha256:
                    raise ValueError("benchmark v9 base evidence digest mismatch")
                if self.composite != (
                    self.base_evidence.effective_composite_micros / 1_000_000
                ):
                    raise ValueError("benchmark v9 base evidence contradicts composite")
        elif self.base_evidence_sha256 is not None:
            raise ValueError("base evidence digest is only valid for benchmark v9")
        elif self.base_evidence is not None:
            raise ValueError("base evidence is only valid for benchmark v9")
        return self


class V9ConfirmationReceipt(BaseModel):
    """Separate signed evidence and subject projection for v9 rewards.

    This never replaces :attr:`LedgerEntry.composite`: the ordinary score and
    its quorum receipts stay byte-for-byte intact.  Validators verify this
    bundle signature, replay the typed arithmetic, and only then consume
    ``full_effective_micros`` in the enforce-mode v9 fold.
    """

    # UUIDs and datetimes arrive as JSON strings over HTTP. Keep the wire model
    # parseable from ``response.json()``; scalar score fields below remain
    # strict integers and the signed root uses its own strict schema.
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["enforce"]
    result_status: Literal["full_confirmed"]
    qualification_status: Literal["qualified"]
    bundle_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    reporter_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bundle_signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]
    evidence_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    evidence_root: V9ConfirmationEvidenceRoot
    base_evidence_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    base_quality_micros: Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    base_stderr_micros: Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    base_model_factor_bps: Literal[0, 10_000]
    base_tool_factor_bps: Literal[0, 10_000]
    full_quality_micros: Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    full_stderr_micros: Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    semantic_factor_bps: Literal[0, 10_000]
    applied_factor_bps: Literal[0, 10_000]
    full_effective_micros: Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    verified_at: datetime


class LedgerEntry(BaseModel):
    """One miner's best eligible score, returned by ``GET /scoring/scores``.

    The public score pool (PROJECT.md D3) the validator folds into KOTH+ATH
    weights. One entry per active miner = that miner's highest-scoring eligible
    agent (status ``scored``). ``first_seen`` is the tie-break that lets the
    original beat a later copy of the same score; ``composite`` is the raw
    reported double (never rounded, so every validator folds identical bytes).
    ``signature`` is the reporting validator's sr25519 signature so the ledger is
    self-verifying.
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
        Field(
            description=(
                "The KOTH first-seen tie-break (UTC): when this miner's lineage "
                "first reached the score this entry defends, which is the "
                "entry's own upload time unless an earlier generation of the "
                "same owner already held a band-equivalent score. Anchoring on "
                "the entry alone made a miner forfeit its crown by resubmitting."
            )
        ),
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
                "During a rollout the ledger may contain a mix: an agent switches "
                "to the desired version only after reaching quorum. Additive and "
                "optional so older validators safely ignore it."
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
                "All validator-signed receipts backing the platform median. "
                "Validators verify these independently before folding weights."
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
                "score report carried one. The validator's KOTH fold uses it for "
                "the measurement-uncertainty indifference band (dethrone only when "
                "a challenger's lead exceeds z*sqrt(se_c^2 + se_champ^2)); absent "
                "means the fold falls back to the fixed composite-point margin. "
                "Additive-optional, mirroring bench_version."
            ),
        ),
    ]
    confirmation_composites: Annotated[
        list[float] | None,
        Field(
            default=None,
            description=(
                "Per-seed composites for this agent from a version-bump re-score "
                "over K common CRN seeds (prod hardening P4), if the winning score "
                "report carried them. With two or more values the validator's KOTH "
                "fold dethrones on their median instead of the single-run "
                "composite, so a crown flip must replicate across seeds. "
                "Additive-optional; absent means the fold uses the raw composite."
            ),
        ),
    ]
    confirmation_seeds: Annotated[
        list[int] | None,
        Field(
            default=None,
            description=(
                "The K common CRN seeds aligned 1:1 with "
                "``confirmation_composites`` for this agent's version-bump "
                "re-score, if the winning score report carried them. Lets the "
                "validator's KOTH fold pair a challenger against the champion on "
                "shared seeds (lower paired-difference variance) instead of the "
                "independent-sum band. Additive-optional; absent means the fold "
                "uses the unpaired band."
            ),
        ),
    ]
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
            "Activation marker for validator protocol v14+. When present, the "
            "weight fold uses the arithmetic mean of the three signed quorum "
            "scores plus one aggregate per completed continual cohort wave. "
            "Older validators ignore this additive field."
        ),
    )
    efficiency_bonus: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=0.1,
            description=(
                "Frozen platform-side relative token-efficiency bonus fraction "
                "for this entry (bench_version >= 7). Populated only while the "
                "platform's DITTO_EFFICIENCY_BONUS_FOLD_ENABLED flag is on; "
                "absent otherwise so existing folds are byte-identical. "
                "Advisory until the subnet's weight fold ships a consensus "
                "change that consumes it — a validator must never fold this "
                "field unilaterally."
            ),
        ),
    ] = None
    efficiency_factor: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.85,
            le=1.1,
            description=(
                "Frozen platform-side bounded efficiency factor for this "
                "entry. Protocol 21 keeps authoritative Bench-v9 quality as "
                "the primary order and uses curve-v3's adjusted projection only "
                "to break exact-quality ties. "
                "Populated only while the coordinated efficiency fold is active "
                "and every recently-live weight-setting validator reports "
                "heartbeat protocol 21+, regardless of scorer capability; "
                "when present it supersedes efficiency_bonus."
            ),
        ),
    ] = None
    effective_composite: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.1,
            description=(
                "Platform-projected efficiency tiebreak with the frozen "
                "adjustment applied; curve-v3 upside scales remaining headroom. "
                "It is considered only after exact authoritative-quality "
                "equality. Validators independently derive it from "
                "the authoritative quality evidence and efficiency_factor (or "
                "legacy efficiency_bonus); signed evidence is never modified."
            ),
        ),
    ] = None
    v9_confirmation: Annotated[
        V9ConfirmationReceipt | None,
        Field(
            default=None,
            exclude_if=lambda value: value is None,
            description=(
                "Separate signed full-confirmation receipt. Present only for "
                "fully confirmed Bench v9 rows while enforce mode is active."
            ),
        ),
    ] = None
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
                "first; the selected generation's hotkey is the weight destination."
            )
        ),
    ]
    active_bench_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Platform-authoritative benchmark version for autonomous retest "
                "planning. None is reserved for rolling compatibility with a "
                "pre-field Platform response."
            ),
        ),
    ] = None
    v9_confirmation_mode: Annotated[
        Literal["enforce"] | None,
        Field(
            default=None,
            exclude_if=lambda value: value is None,
            description=(
                "Fail-closed marker: every Bench v9 entry must carry a valid "
                "full-confirmation receipt while present."
            ),
        ),
    ] = None
    tie_weighting_mode: Annotated[
        Literal["pool"] | None,
        Field(
            default=None,
            exclude_if=lambda value: value is None,
            description=(
                "Consensus activation marker for tie-aware rank-share pooling. "
                "When set to pool, exact effective-score ties share the slots "
                "they occupy; non-exact ties require valid paired shared-seed "
                "evidence. Absent keeps the historical fixed rank shares."
            ),
        ),
    ] = None
    count: Annotated[int, Field(ge=0, description="Number of entries returned.")]
    generated_at: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "When these entries were read from the DB (UTC). On a served "
                "last-known-good snapshot this is the age of the cached read, not "
                "'now'."
            ),
        ),
    ] = None
    stale: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True when the live DB read failed and this is a served "
                "last-known-good snapshot. A fold may still use it (the ledger is "
                "durable and slow-moving) but should treat it as advisory."
            ),
        ),
    ] = False
    age_seconds: Annotated[
        int,
        Field(
            default=0,
            ge=0,
            description="Age of the snapshot in seconds (0 on a fresh read).",
        ),
    ] = 0
    burn_share: Annotated[
        float,
        Field(
            default=0.0,
            ge=0.0,
            le=1.0,
            description=(
                "Share of miner emission the fold must route to the subnet "
                "owner's burn hotkey; the remainder is normalized across the "
                "eligible miner weights. Operator-owned and resolved here, so "
                "every validator reads one already-decided scalar rather than a "
                "schedule it has to evaluate against its own clock. ``0.0`` (the "
                "default, and what an older platform's omission means) releases "
                "the full miner emission through KOTH, which is what the "
                "validator's frozen MINER_EMISSION_SHARE already does -- so a "
                "validator that ignores this field keeps folding exactly as it "
                "did. On a served last-known-good snapshot this is the share "
                "that was current when the snapshot was taken."
            ),
        ),
    ] = 0.0
    continual_retest_cohort_size: Annotated[
        int,
        Field(
            default=5,
            ge=5,
            description=(
                "How many ranked agents the operator currently has the continual "
                "retest lane covering: 5 (the emission set) up to 25. Advisory "
                "planning input for the validator's shared-seed round — the "
                "platform still enforces membership when it issues the lease, so "
                "a validator that ignores this field simply keeps planning the "
                "top five and loses nothing but the extra coverage. Emissions, "
                "the weight fold, and wave completion are always the top five, "
                "whatever this says."
            ),
        ),
    ] = 5

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "generated_at": "2026-06-08T12:00:00Z",
                "burn_share": 0.0,
                "tie_weighting_mode": None,
                "continual_retest_cohort_size": 5,
                "stale": False,
                "age_seconds": 0,
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

    ``status`` is the agent's lifecycle state *after* recording the score:
    ``scored`` once the first score finalizes an ``evaluating`` agent, and
    unchanged (``live``) when a validator re-scores a promoted agent.
    ``accepted`` is ``True`` when the report was persisted; it leaves room
    for a future soft-reject (e.g. a stale report) without changing the
    status code.
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


class SubmitTranscriptResponse(BaseModel):
    """Response of ``PUT /validator/agent/{agent_id}/transcript/{run_id}``.

    ``stored`` is ``False`` only when the public bucket is unconfigured (the
    upload was accepted and verified but has nowhere public to land); a digest
    mismatch is a 409, never a silent drop.
    """

    agent_id: Annotated[UUID, Field(description="Echoes the path-param id.")]
    run_id: Annotated[str, Field(description="Echoes the path-param run id.")]
    transcript_sha256: Annotated[
        str,
        Field(description="SHA-256 hex digest of the stored transcript bytes."),
    ]
    stored: Annotated[
        bool,
        Field(
            description=(
                "``True`` when the artifact now exists in the public bucket "
                "(content-addressed at ``transcripts/{sha256}.json``)."
            )
        ),
    ]
