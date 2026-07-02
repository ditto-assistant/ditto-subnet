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
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.upload import (
    _SIGNATURE_HEX_PATTERN,
    _SS58_PATTERN,
)


class ValidatorQueueItem(BaseModel):
    """One agent awaiting evaluation, returned by ``GET /validator/queue``.

    Carries exactly what the daemon needs to fetch + identify the
    submission; the tarball itself comes from the ``/artifact`` endpoint.
    """

    agent_id: Annotated[UUID, Field(description="Server-generated agent identifier.")]
    miner_hotkey: Annotated[str, Field(description="Submitting miner's SS58 hotkey.")]
    name: Annotated[str, Field(description="Miner-chosen agent name.")]
    sha256: Annotated[
        str, Field(description="SHA-256 of the uploaded tarball, lowercase hex.")
    ]
    status: Annotated[
        AgentStatus, Field(description="Lifecycle state at queue read time.")
    ]
    created_at: Annotated[
        datetime, Field(description="When the upload row was inserted (UTC).")
    ]


class ValidatorQueueResponse(BaseModel):
    """Returned by ``GET /validator/queue``.

    ``items`` is ordered oldest-first so a daemon draining the queue
    processes submissions roughly in arrival order. ``count`` echoes
    ``len(items)`` for convenience.
    """

    items: Annotated[
        list[ValidatorQueueItem],
        Field(description="Agents awaiting evaluation, oldest first."),
    ]
    count: Annotated[int, Field(ge=0, description="Number of items returned.")]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {
                        "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                        "miner_hotkey": (
                            "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
                        ),
                        "name": "alpha-agent",
                        "sha256": "deadbeef" * 8,
                        "status": "screening_passed",
                        "created_at": "2026-06-08T12:00:00Z",
                    }
                ],
                "count": 1,
            }
        }
    )


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

    @field_validator("called", "expected", "notes", mode="before")
    @classmethod
    def _none_to_empty(cls, v: list[str] | None) -> list[str]:
        # The Go scorer omits or nulls these on cases that have none (memory
        # cases carry no expected tools); coerce null/absent to an empty list.
        return v if v is not None else []


class ScoreReport(BaseModel):
    """A completed DittoBench evaluation result for one agent.

    Mirrors the Go validator's ``ScoreReport`` so the scoring engine's
    output round-trips through ``POST /validator/agent/{id}/score``
    unchanged. ``composite = 0.6*tool_mean + 0.4*memory_mean`` when both
    kinds are present (the platform does not recompute it; it records
    what the daemon reports).
    """

    run_id: Annotated[str, Field(description="Scoring-engine run identifier.")]
    seed: Annotated[
        int, Field(description="Dataset seed used (anti-overfit reproducibility).")
    ]
    composite: Annotated[
        float, Field(ge=0.0, le=1.0, description="Aggregate score in [0,1].")
    ]
    tool_mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean tool accuracy in [0,1].")
    ]
    memory_mean: Annotated[
        float, Field(ge=0.0, le=1.0, description="Mean memory recall in [0,1].")
    ]
    median_ms: Annotated[int, Field(ge=0, description="Median per-case latency (ms).")]
    n: Annotated[int, Field(ge=0, description="Number of cases scored.")]
    generated_at: Annotated[
        datetime, Field(description="When the report was produced (UTC).")
    ]
    per_case: Annotated[
        list[CaseScore],
        Field(default_factory=list, description="Optional per-case breakdown."),
    ]


class SubmitScoreRequest(BaseModel):
    """Body of ``POST /validator/agent/{agent_id}/score``.

    The validator authenticates by signing the report it submits: the
    signature is over the UTF-8 bytes of ``f"{validator_hotkey}:{run_id}"``
    with the validator's hotkey keypair. (Signature verification is
    deferred to the real-auth pass; the field is carried now so the wire
    contract is stable.)
    """

    validator_hotkey: Annotated[
        str,
        Field(pattern=_SS58_PATTERN, description="Reporting validator's SS58 hotkey."),
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description="Hex sr25519 signature over ``{validator_hotkey}:{run_id}``.",
        ),
    ]
    report: Annotated[ScoreReport, Field(description="The DittoBench score report.")]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "validator_hotkey": (
                    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
                ),
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


class LedgerEntry(BaseModel):
    """One miner's best eligible score, returned by ``GET /scoring/scores``.

    The public score pool (PROJECT.md D3) the validator folds into KOTH+ATH
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
        float, Field(ge=0.0, le=1.0, description="Best composite score in [0,1].")
    ]
    first_seen: Annotated[
        datetime,
        Field(description="Agent upload time (UTC); the KOTH first-seen tie-break."),
    ]
    sha256: Annotated[
        str, Field(description="SHA-256 of the tarball, lowercase hex.")
    ]
    size_bytes: Annotated[
        int | None, Field(default=None, ge=0, description="Tarball size in bytes.")
    ]
    seed: Annotated[int, Field(description="Dataset seed of the scoring run.")]
    validator_hotkey: Annotated[
        str, Field(description="SS58 hotkey of the validator that produced the score.")
    ]
    signature: Annotated[
        str | None,
        Field(
            default=None,
            description="Validator's hex sr25519 signature, if stored.",
        ),
    ]
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
        Field(description="Best eligible score per miner, highest composite first."),
    ]
    count: Annotated[int, Field(ge=0, description="Number of entries returned.")]

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
                        "first_seen": "2026-06-08T12:00:00Z",
                        "sha256": "deadbeef" * 8,
                        "size_bytes": 524288,
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
