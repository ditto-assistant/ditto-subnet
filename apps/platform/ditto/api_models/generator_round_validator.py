"""Validator-facing wire models for generator-competition round panel runs.

A generator round (see ``ditto.api_models.generator_rounds`` in Platform) is
executed by validators: Platform leases one panel agent at a time on the
agent's own ``validator_tickets`` row with purpose ``generator_round``, the
validator fetches the round dataset through the private-dataset route, runs
the screened image on it with dittobench-api's ``generator-round-v1`` mode,
and posts per-case results here. Nothing in this lane writes an agent score.

The same module carries the round-commitment lane: Platform elects a few
validators per committed round to publish the round's seed commitment
(``dgr1:{round}:{commitment}``) with ``Commitments.set_commitment`` from their
own Pylon identity, and each reports the block it observed the value at.

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
GENERATOR_ROUND_COMMITMENT_JOB_DOMAIN = "validator-generator-round-commitment-job:v1"
GENERATOR_ROUND_COMMITMENT_REPORT_DOMAIN = (
    "validator-generator-round-commitment-report-v1"
)
ROUND_COMMITMENT_TAG = "dgr1"
# ``Commitments.set_commitment`` accepts at most 128 bytes of raw data.
MAX_COMMITMENT_PAYLOAD_BYTES = 128
# A round holds at most MAX_ROUND_COLUMNS columns of MAX_ROUND_COLUMN_CASES
# cases (the pack contract's challenge.MaxRoundColumns and challenge.MaxCases).
# LEGACY_ROUND_COLUMN_CASES is the column size a scorer that does not advertise
# ``generator-round-max-cases-<n>`` is assumed to run: validators released with
# the old 8 x 32 bound refuse a larger job, so Platform leases them none.
MAX_ROUND_COLUMNS = 8
MAX_ROUND_COLUMN_CASES = 80
LEGACY_ROUND_COLUMN_CASES = 32
MAX_ROUND_CASES = MAX_ROUND_COLUMNS * MAX_ROUND_COLUMN_CASES
GENERATOR_ROUND_MAX_CASES_FEATURE_PREFIX = "generator-round-max-cases-"
# Bench versions a generator round can run at. A round freezes one of these
# when it opens (the active version when it is listed, else the newest listed
# version below it) and keeps it until it is scored. The set is the pack
# contract's grading policies (``challenge.SupportedGradingBenchVersions``): a
# round at version N accepts packs graded at the listed versions in its window
# (``accepted_grading_versions``: N-1 and N), each column at the policy its
# pack declares, so a generator that predates v14 keeps its v13 column in a
# v14 round. Every activatable bench version from the first round version on
# has an entry: one number for both competitions, and
# ``test_bench_version_pins`` fails on a shipped benchmark contract without
# one. dittobench-api's ``generator-round-v1`` mode runs a round at every
# listed version and advertises each as ``generator-round-bench-<n>``.
# ``ditto/tests/test_bench_version_pins.py`` diffs this tuple against the Go
# pack contract.
GENERATOR_ROUND_BENCH_VERSIONS: tuple[int, ...] = (13, 14)
# The scorer feature naming one round bench version, and the versions a scorer
# that advertises none runs (the original v13-only round mode).
GENERATOR_ROUND_BENCH_FEATURE_PREFIX = "generator-round-bench-"
LEGACY_SCORER_ROUND_BENCH_VERSIONS: tuple[int, ...] = (13,)
# How many grading policies a round accepts: its own version and the one
# before it (``challenge.GradingPolicyWindow``). With {13, 14} that is every
# policy; once a third exists a round at N accepts only N-1 and N.
GRADING_POLICY_WINDOW = 2
# The version every round opened before rounds recorded one ran at.
LEGACY_ROUND_BENCH_VERSION = 13


def accepted_grading_versions(round_bench_version: int) -> tuple[int, ...]:
    """The pack grading policies a round frozen at ``round_bench_version``
    accepts, oldest first (``challenge.GradingBenchVersionsThrough``)."""
    return tuple(
        version
        for version in GENERATOR_ROUND_BENCH_VERSIONS
        if round_bench_version - GRADING_POLICY_WINDOW < version <= round_bench_version
    )


GeneratorRoundFailReason = Literal["scoring_error", "infrastructure"]
GeneratorRoundCommitmentOutcome = Literal["confirmed", "failed"]

_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{48}$"
_SHA256 = r"^[0-9a-f]{64}$"
_SIG = r"^[0-9a-f]{128}$"
_EXTRINSIC_HASH = r"^0x[0-9a-f]{64}$"


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


# --- round commitment publication -------------------------------------------


def round_commitment_payload(round_index: int, commitment: str) -> str:
    """The exact ASCII value published with ``Commitments.set_commitment``."""
    return f"{ROUND_COMMITMENT_TAG}:{round_index}:{commitment}"


class GeneratorRoundCommitmentJobRequest(_Timed):
    """Ask Platform whether this validator should publish a round commitment."""

    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIG)] = "0" * 128

    def signing_message(self) -> bytes:
        return (
            f"{GENERATOR_ROUND_COMMITMENT_JOB_DOMAIN}:{self.validator_hotkey}:"
            f"{self.nonce}:{_iso(self.requested_at)}"
        ).encode()


class GeneratorRoundCommitmentJobResponse(_Model):
    """One committed round this validator is elected to publish on-chain.

    ``payload`` is exactly ``round_commitment_payload(round_index,
    commitment)``; a validator refuses any other value. ``claim_expires_at`` is
    when Platform stops waiting for this validator's report and lets the next
    validator in the round's publisher order take its place.
    """

    round_index: Annotated[int, Field(ge=0)]
    commitment: Annotated[str, Field(pattern=_SHA256)]
    payload: Annotated[
        str, Field(min_length=1, max_length=MAX_COMMITMENT_PAYLOAD_BYTES)
    ]
    claim_expires_at: datetime

    @model_validator(mode="after")
    def _payload_is_the_commitment(self):  # type: ignore[no-untyped-def]
        if self.payload != round_commitment_payload(self.round_index, self.commitment):
            raise ValueError("payload is not this round's commitment")
        return self


class GeneratorRoundCommitmentReportRequest(_Timed):
    """Signed outcome of one elected round-commitment publication.

    ``confirmed`` means the validator read its own on-chain commitment back
    and it equals the payload; ``block_number`` is the block the chain stamped
    it at. ``failed`` means the submission or the read-back did not succeed.
    """

    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    round_index: Annotated[int, Field(ge=0)]
    commitment: Annotated[str, Field(pattern=_SHA256)]
    outcome: GeneratorRoundCommitmentOutcome
    block_number: Annotated[int | None, Field(ge=1)] = None
    extrinsic_hash: Annotated[str | None, Field(pattern=_EXTRINSIC_HASH)] = None
    detail: Annotated[str | None, Field(max_length=500)] = None
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIG)] = "0" * 128

    @model_validator(mode="after")
    def _confirmed_has_a_block(self):  # type: ignore[no-untyped-def]
        if self.outcome == "confirmed" and self.block_number is None:
            raise ValueError("a confirmed publication names its block")
        return self

    def signing_message(self) -> bytes:
        return json.dumps(
            [
                GENERATOR_ROUND_COMMITMENT_REPORT_DOMAIN,
                self.validator_hotkey,
                self.round_index,
                self.commitment,
                self.outcome,
                self.block_number,
                self.extrinsic_hash,
                str(self.nonce),
                _iso(self.requested_at),
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()


class GeneratorRoundCommitmentReportResponse(_Model):
    round_index: int
    accepted: bool
    duplicate: bool = False
    confirmations: int = 0
    target: int = 0
