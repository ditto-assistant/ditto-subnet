"""Execute sealed V13 paired cases through a trusted isolated harness boundary.

This module deliberately emits only aggregate, digest-bound evidence. A
production adapter must create a fresh sandbox and broker ledger per side;
missing model-authority evidence or any execution fault is inconclusive.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol.v13_private_package import (
    PreparedV13PrivatePackage,
    SealedPackageStore,
    V13PrivateRunSummary,
    _prepare_registered_package,
)

_SHA_PATTERN = r"^[0-9a-f]{64}$"
_MAX_RESPONSE_BYTES = 64 * 1024


class PrivateCasePayload(BaseModel):
    """One protected, operator-reviewed deterministic task composition."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-case-v1"] = "v13-private-case-v1"
    pair_id: UUID
    side: Literal["control", "variant"]
    semantic_contract_sha256: str = Field(pattern=_SHA_PATTERN)
    seed_envelope: dict[str, object] | None = None
    run_envelope: dict[str, object]
    expected_answer_sha256: str = Field(pattern=_SHA_PATTERN)


class IsolatedCaseObservation(BaseModel):
    """Private result returned by a fresh trusted sandbox/broker session."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    response: dict[str, object]
    model_authority_verified: bool


class FreshIsolatedCaseExecutor(Protocol):
    """Each call must use a new state, network, container, and broker ledger."""

    async def run_case(
        self,
        *,
        image_sha256: str,
        seed_envelope: Mapping[str, object] | None,
        run_envelope: Mapping[str, object],
        timeout_seconds: float,
    ) -> IsolatedCaseObservation: ...


class PrivatePairCounts(BaseModel):
    """Sanitized aggregate; no prompts, answers, case IDs, or hidden seeds."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    transformation_class: str
    seed_commitment: str = Field(pattern=_SHA_PATTERN)
    pairs: int = Field(ge=0)
    control_correct: int = Field(ge=0)
    variant_correct: int = Field(ge=0)
    control_only_correct: int = Field(ge=0)
    variant_only_correct: int = Field(ge=0)


class PrivateExecutionResult(BaseModel):
    """Report-only paired outcomes; no materiality decision or policy verdict."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: V13PrivateRunSummary
    aggregates: tuple[PrivatePairCounts, ...]


class PrivateExecutionUnavailable(ValueError):
    """No usable private verification result; leave the check incomplete."""


async def _load_case(
    store: SealedPackageStore, digest: str, pair_id: UUID, side: str
) -> PrivateCasePayload:
    raw = await store.read_payload(digest)
    if len(raw) > 128 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise PrivateExecutionUnavailable("private case commitment mismatch")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or set(parsed) != set(
        PrivateCasePayload.model_fields
    ):
        raise PrivateExecutionUnavailable("private case shape unavailable")
    case = PrivateCasePayload.model_validate(parsed)
    if case.pair_id != pair_id or case.side != side:
        raise PrivateExecutionUnavailable("private case identity mismatch")
    return case


def _answer_digest(response: Mapping[str, object]) -> str:
    answer = response.get("answer")
    if not isinstance(answer, str):
        answer = response.get("final_text")
    if not isinstance(answer, str):
        raise PrivateExecutionUnavailable("private answer unavailable")
    encoded = answer.encode("utf-8")
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise PrivateExecutionUnavailable("private answer exceeds bound")
    return hashlib.sha256(encoded).hexdigest()


async def execute_v13_private_pairs(
    *,
    prepared: PreparedV13PrivatePackage,
    store: SealedPackageStore,
    executor: FreshIsolatedCaseExecutor,
    runner_hotkey: str,
) -> PrivateExecutionResult:
    """Run every committed pair with fresh isolation and digest-only output.

    This is an execution primitive, not a statistical finding. The caller must
    source the prepared package from a trusted registry and independently
    attest the executor's sandbox and model broker. Any fault fails closed.
    """
    try:
        async with asyncio.timeout(7200):
            # Preflight is only a handoff. Re-read the sealed manifest and every
            # payload at the execution boundary so a stale or forged prepared
            # inventory cannot choose which cases the runner executes.
            current = await _prepare_registered_package(
                store=store,
                commitment=prepared.commitment,
                registration=prepared.registration,
            )
            if current != prepared:
                raise PrivateExecutionUnavailable(
                    "private prepared package commitment mismatch"
                )
            counts: Counter[tuple[str, str, str]] = Counter()
            evidence = hashlib.sha256()
            for pair in prepared.manifest.pairs:
                control = await _load_case(
                    store, pair.control_sha256, pair.pair_id, "control"
                )
                variant = await _load_case(
                    store, pair.variant_sha256, pair.pair_id, "variant"
                )
                if control.semantic_contract_sha256 != variant.semantic_contract_sha256:
                    raise PrivateExecutionUnavailable(
                        "private paired semantics commitment mismatch"
                    )
                outcomes: list[bool] = []
                for case in (control, variant):
                    observation = await executor.run_case(
                        image_sha256=prepared.commitment.image_sha256,
                        seed_envelope=case.seed_envelope,
                        run_envelope=case.run_envelope,
                        timeout_seconds=30.0,
                    )
                    if not observation.model_authority_verified:
                        raise PrivateExecutionUnavailable(
                            "private model authority unavailable"
                        )
                    outcomes.append(
                        _answer_digest(observation.response)
                        == case.expected_answer_sha256
                    )
                control_ok, variant_ok = outcomes
                key = (pair.transformation_class, pair.seed_commitment)
                counts[(*key, "pairs")] += 1
                counts[(*key, "control_correct")] += int(control_ok)
                counts[(*key, "variant_correct")] += int(variant_ok)
                counts[(*key, "control_only_correct")] += int(
                    control_ok and not variant_ok
                )
                counts[(*key, "variant_only_correct")] += int(
                    variant_ok and not control_ok
                )
                evidence.update(pair.pair_id.bytes)
                evidence.update(bytes((int(control_ok), int(variant_ok))))
            aggregate_keys = sorted({key[:2] for key in counts})
            aggregates = tuple(
                PrivatePairCounts(
                    transformation_class=class_name,
                    seed_commitment=seed_commitment,
                    pairs=counts[(class_name, seed_commitment, "pairs")],
                    control_correct=counts[
                        (class_name, seed_commitment, "control_correct")
                    ],
                    variant_correct=counts[
                        (class_name, seed_commitment, "variant_correct")
                    ],
                    control_only_correct=counts[
                        (class_name, seed_commitment, "control_only_correct")
                    ],
                    variant_only_correct=counts[
                        (class_name, seed_commitment, "variant_only_correct")
                    ],
                )
                for class_name, seed_commitment in aggregate_keys
            )
            return PrivateExecutionResult(
                summary=V13PrivateRunSummary(
                    agent_id=prepared.commitment.agent_id,
                    attempt_id=prepared.commitment.attempt_id,
                    artifact_sha256=prepared.commitment.artifact_sha256,
                    image_sha256=prepared.commitment.image_sha256,
                    profile_sha256=prepared.registration.profile_sha256,
                    manifest_sha256=prepared.registration.manifest_sha256,
                    runner_hotkey=runner_hotkey,
                    status="completed",
                    completed_pairs=len(prepared.manifest.pairs),
                    evidence_sha256=evidence.hexdigest(),
                ),
                aggregates=aggregates,
            )
    except PrivateExecutionUnavailable:
        raise
    except Exception:
        raise PrivateExecutionUnavailable("private execution unavailable") from None
