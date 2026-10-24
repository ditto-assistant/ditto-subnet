"""Validator-facing wire models for generator-competition round panel runs.

A generator round (see ``ditto.api_models.generator_rounds`` in Platform) is
executed by validators: Platform leases one panel agent at a time on the
agent's own ``validator_tickets`` row with purpose ``generator_round``, the
validator fetches the round dataset through the private-dataset route, runs
the screened image on it with dittobench-api's ``generator-round-v1`` mode,
and posts per-case results here. Nothing in this lane writes an agent score.

This module is a hand-maintained copy in the validator package
(``ditto/api_models/generator_round_validator.py``) and Platform
(``apps/platform/ditto/api_models/generator_round_validator.py``); the
validator contract golden pins their structure.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.inference import InferenceGrantOffer

GENERATOR_ROUND_DATASET_MODE: Literal["generator-round-v1"] = "generator-round-v1"
GENERATOR_ROUND_JOB_DOMAIN = "validator-generator-round-job:v1"
GENERATOR_ROUND_SCORE_DOMAIN = "validator-generator-round-score-v1"
GENERATOR_ROUND_FAIL_DOMAIN = "validator-generator-round-fail-v1"
MAX_ROUND_CASES = 8 * 32

GeneratorRoundFailReason = Literal["scoring_error", "infrastructure"]

_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{48}$"
_SHA256 = r"^[0-9a-f]{64}$"
_SIG = r"^[0-9a-f]{128}$"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Timed(_Model):
    @model_validator(mode="after")
    def _aware(self):  # type: ignore[no-untyped-def]
        for name in ("requested_at", "ticket_deadline"):
            value = getattr(self, name, None)
            if isinstance(value, datetime) and value.tzinfo is None:
                raise ValueError(f"{name} requires a timezone")
        return self


class GeneratorRoundJobRequest(_Timed):
    """Ask Platform for one round panel run on a free slot."""

    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    slot_id: Annotated[str, Field(pattern=r"^slot-[0-7]$")]
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIG)] = "0" * 128

    def signing_message(self) -> bytes:
        return (
            f"{GENERATOR_ROUND_JOB_DOMAIN}:{self.validator_hotkey}:"
            f"{self.slot_id}:{self.nonce}:{_iso(self.requested_at)}"
        ).encode()


class GeneratorRoundJobResponse(_Model):
    """One leased round panel run.

    ``seed`` is a positive placeholder the scorer requires on ``/v2/score``;
    the round dataset, not the seed, determines every case.
    """

    round_index: Annotated[int, Field(ge=0)]
    agent_id: UUID
    slot_id: str
    miner_hotkey: str
    sha256: Annotated[str, Field(pattern=_SHA256)]
    deadline: datetime
    bench_version: int
    dataset_sha256: Annotated[str, Field(pattern=_SHA256)]
    dataset_mode: Literal["generator-round-v1"] = GENERATOR_ROUND_DATASET_MODE
    seed: Annotated[int, Field(ge=1)]
    run_size: str = "full"
    case_count: Annotated[int, Field(ge=1, le=MAX_ROUND_CASES)]
    minimum_screening_policy_version: int | None = None
    requires_screened_image: bool = True
    benchmark_runtime: dict | None = None
    """Mirrors ``BenchmarkRuntimeSettings`` (forwarded verbatim to the scorer)."""
    inference: InferenceGrantOffer | None = None


class GeneratorRoundCaseResult(_Model):
    case_id: Annotated[str, Field(min_length=3, max_length=72)]
    score: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)] = 0.0
    fault: bool = False


def round_results_digest(results: list[GeneratorRoundCaseResult]) -> str:
    """SHA-256 of the canonical results the score signature covers."""
    body = json.dumps(
        [[r.case_id, repr(float(r.score)), bool(r.fault)] for r in results],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(body).hexdigest()


class GeneratorRoundScoreRequest(_Timed):
    """Signed per-case results of one round panel run."""

    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    round_index: Annotated[int, Field(ge=0)]
    ticket_deadline: datetime
    dataset_sha256: Annotated[str, Field(pattern=_SHA256)]
    run_id: Annotated[str, Field(min_length=1, max_length=200)]
    results: Annotated[
        list[GeneratorRoundCaseResult], Field(min_length=1, max_length=MAX_ROUND_CASES)
    ]
    signature: Annotated[str, Field(pattern=_SIG)] = "0" * 128

    def signing_message(self, agent_id: UUID) -> bytes:
        return json.dumps(
            [
                GENERATOR_ROUND_SCORE_DOMAIN,
                self.validator_hotkey,
                str(agent_id),
                self.round_index,
                _iso(self.ticket_deadline),
                self.dataset_sha256,
                self.run_id,
                round_results_digest(self.results),
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()


class GeneratorRoundScoreResponse(_Model):
    round_index: int
    agent_id: UUID
    accepted: bool
    duplicate: bool = False


class GeneratorRoundFailRequest(_Timed):
    """Hand a round panel run back without charging the agent an attempt."""

    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    round_index: Annotated[int, Field(ge=0)]
    ticket_deadline: datetime
    reason: GeneratorRoundFailReason
    detail: Annotated[str | None, Field(max_length=500)] = None
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIG)] = "0" * 128

    def signing_message(self, agent_id: UUID) -> bytes:
        return json.dumps(
            [
                GENERATOR_ROUND_FAIL_DOMAIN,
                self.validator_hotkey,
                str(agent_id),
                self.round_index,
                _iso(self.ticket_deadline),
                self.reason,
                str(self.nonce),
                _iso(self.requested_at),
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()


class GeneratorRoundFailResponse(_Model):
    round_index: int
    agent_id: UUID
    released: bool
