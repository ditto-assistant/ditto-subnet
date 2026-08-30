"""Validator hotkey loading + score signing.

The worker signs each score submission so the platform can verify the report
came from the claimed validator hotkey *and* that its contents were not tampered
with. The signature binds a **canonical payload** — the validator hotkey, the
agent id, and the reported ``run_id`` / ``composite`` / ``seed`` — so a captured
signature cannot be replayed against a different agent, and the composite the
platform records cannot be altered without invalidating the signature. (The
platform's ``/validator/.../score`` rebuilds the same string and verifies it.)

The signing private key comes from an operator-provisioned Bittensor wallet on
the host. Validator runtime code has no cloud credential or Secret Manager
dependency. Never log the private key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ditto.api_models.benchmark_capacity import (
    BenchmarkCapacity,
    benchmark_capacity_signing_token,
)
from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    benchmark_progress_signing_token,
)
from ditto.api_models.coding import (
    CodingCapabilityCertificationReceipt,
    coding_authoring_freeze_signing_message,
    coding_authoring_lease_signing_message,
    coding_certification_signing_message,
    coding_grading_lease_signing_message,
    coding_shadow_result_signing_message,
)
from ditto.api_models.coding_harness import coding_harness_launch_signing_message
from ditto.api_models.coding_inference_grants import (
    coding_inference_exchange_signing_message,
    coding_inference_grant_signing_message,
    coding_inference_revoke_signing_message,
)
from ditto.api_models.confirmation_progress import (
    ConfirmationProgress,
    confirmation_progress_signing_token,
)
from ditto.api_models.stack_health import (
    ValidatorStackHealth,
    validator_stack_health_signing_token,
)
from ditto.api_models.system_health import (
    SystemMetrics,
    system_metrics_signing_token,
)
from ditto.api_models.validator_capabilities import (
    ValidatorCapabilities,
    ValidatorStackIdentity,
    validator_identity_signing_token,
)
from ditto.api_models.validator_updater import (
    ValidatorUpdaterStatus,
    validator_updater_status_signing_token,
)
from ditto.validator.errors import ValidatorConfigError
from ditto_screening_protocol.bench_v9 import supports_confirmation
from ditto_screening_protocol.confirmation import canonical_json
from ditto_screening_protocol.confirmation_wire import (
    ConfirmationWireError,
    completion_report_from_go_dimensions,
)

if TYPE_CHECKING:
    from ditto.api_models.validator import LedgerEntry, LedgerScoreProof
    from ditto.api_models.validator_confirmation import (
        V9ConfirmationJobResponse,
        V9ConfirmationPreparedReport,
        V9ConfirmationScorerResult,
    )
    from ditto.validator.config import ValidatorConfig


def load_validator_keypair(config: ValidatorConfig) -> Any:
    """Load the signing keypair and assert it matches ``config.validator_hotkey``.

    Loads the named bittensor wallet hotkey. Raises if it is not usable or the
    loaded ss58 does not match the configured hotkey (guards against signing
    weights with the wrong key).
    """
    import bittensor

    keypair: Any
    if config.wallet_name and config.wallet_hotkey:
        wallet = bittensor.Wallet(name=config.wallet_name, hotkey=config.wallet_hotkey)
        keypair = wallet.hotkey
    else:  # pragma: no cover - guarded earlier by config parsing
        raise ValidatorConfigError("no signing key source configured")

    if keypair.ss58_address != config.validator_hotkey:
        raise ValidatorConfigError(
            "loaded signing key ss58 does not match VALIDATOR_HOTKEY "
            f"({keypair.ss58_address} != {config.validator_hotkey})"
        )
    return keypair


def score_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime | None = None,
    run_id: str,
    composite: float,
    seed: int,
    bench_version: int | None = None,
    transcript_sha256: str | None = None,
    base_evidence_sha256: str | None = None,
) -> bytes:
    """Build the canonical bytes a score signature is computed over.

    ``{validator_hotkey}:{agent_id}:{ticket_deadline}:{run_id}:``
    ``{composite!r}:{seed}`` — then optional ``:{bench_version}``, followed by
    optional ``:{transcript_sha256}`` when the report declares a transcript
    digest (``details["transcript_sha256"]``). Benchmark v9 requires that
    digest and then appends ``:{base_evidence_sha256}``, so
    the published transcript artifact cannot be swapped without breaking the
    signature. The platform derives presence from the same report field, so a
    report without a transcript keeps the previous format. The exact ticket
    deadline is the lease identity; platform reconstructs this exact string
    from the request to verify, so both sides MUST format it identically — in
    particular ``composite`` uses Python's shortest round-trip float repr,
    which the JSON transport preserves.
    """
    lease = (
        ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
        if ticket_deadline is not None
        else ""
    )
    message = f"{validator_hotkey}:{agent_id}:{lease}:{run_id}:{composite!r}:{seed}"
    if bench_version is not None:
        message += f":{bench_version}"
    if bench_version == 9:
        if not transcript_sha256 or not base_evidence_sha256:
            raise ValueError(
                "benchmark v9 score signatures require transcript and "
                "base evidence digests"
            )
    elif bench_version is None or bench_version < 9:
        if base_evidence_sha256 is not None:
            raise ValueError("base evidence digest is only valid for benchmark v9+")
    elif base_evidence_sha256 is not None and not transcript_sha256:
        # v10+ evidence binds the transcript digest; signing one without the
        # other would produce an unverifiable receipt.
        raise ValueError("base evidence digest requires a transcript digest")
    if transcript_sha256:
        message += f":{transcript_sha256}"
    if base_evidence_sha256:
        message += f":{base_evidence_sha256}"
    return message.encode()


def sign_score(
    keypair: Any,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime | None = None,
    run_id: str,
    composite: float,
    seed: int,
    bench_version: int | None = None,
    transcript_sha256: str | None = None,
    base_evidence_sha256: str | None = None,
) -> str:
    """Return the hex sr25519 signature over the canonical score payload."""
    message = score_signing_message(
        validator_hotkey=validator_hotkey,
        agent_id=agent_id,
        ticket_deadline=ticket_deadline,
        run_id=run_id,
        composite=composite,
        seed=seed,
        bench_version=bench_version,
        transcript_sha256=transcript_sha256,
        base_evidence_sha256=base_evidence_sha256,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()


def sign_coding_certification(
    keypair: Any,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    ticket_deadline: datetime,
    screened_image_sha256: str,
    receipt: CodingCapabilityCertificationReceipt,
) -> str:
    """Sign one shadow coding receipt against the exact Platform lease."""

    message = coding_certification_signing_message(
        validator_hotkey=validator_hotkey,
        agent_id=agent_id,
        bench_version=bench_version,
        ticket_deadline=ticket_deadline,
        screened_image_sha256=screened_image_sha256,
        certification_sha256=receipt.certification_sha256,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()


def sign_coding_authoring_lease(
    keypair: Any,
    *,
    validator_hotkey: str,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Sign one shadow authoring-lease request."""

    message = coding_authoring_lease_signing_message(
        validator_hotkey=validator_hotkey,
        ticket_id=ticket_id,
        nonce=nonce,
        requested_at=requested_at,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()


def sign_coding_inference_grant(
    keypair: Any,
    *,
    validator_hotkey: str,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Sign one shadow coding inference grant request."""

    signature: bytes = keypair.sign(
        coding_inference_grant_signing_message(
            validator_hotkey=validator_hotkey,
            ticket_id=ticket_id,
            nonce=nonce,
            requested_at=requested_at,
        )
    )
    return signature.hex()


def sign_coding_harness_launch(
    keypair: Any,
    *,
    validator_hotkey: str,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Sign one private screened-harness launch request."""

    signature: bytes = keypair.sign(
        coding_harness_launch_signing_message(
            validator_hotkey=validator_hotkey,
            ticket_id=ticket_id,
            nonce=nonce,
            requested_at=requested_at,
        )
    )
    return signature.hex()


def sign_coding_inference_exchange(
    keypair: Any,
    *,
    validator_hotkey: str,
    grant_id: UUID,
    broker_public_key: str,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Sign one shadow coding inference broker exchange."""

    signature: bytes = keypair.sign(
        coding_inference_exchange_signing_message(
            validator_hotkey=validator_hotkey,
            grant_id=grant_id,
            broker_public_key=broker_public_key,
            nonce=nonce,
            requested_at=requested_at,
        )
    )
    return signature.hex()


def sign_coding_inference_revoke(
    keypair: Any,
    *,
    validator_hotkey: str,
    grant_id: UUID,
    generation: int,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Sign one shadow coding inference grant revocation."""

    signature: bytes = keypair.sign(
        coding_inference_revoke_signing_message(
            validator_hotkey=validator_hotkey,
            grant_id=grant_id,
            generation=generation,
            nonce=nonce,
            requested_at=requested_at,
        )
    )
    return signature.hex()


def sign_coding_authoring_freeze(
    keypair: Any,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    run_row_id: UUID,
    ticket_id: UUID,
    ticket_deadline: datetime,
    coding_run_id: str,
    agent_artifact_sha256: str,
    screened_image_sha256: str,
    run_manifest_sha256: str,
    task_set_manifest_sha256: str,
    authoring_evidence_sha256: str,
    authoring_transcript_object_key: str,
    authoring_transcript_bytes: int,
    authoring_event_count: int,
    frozen_submission_object_key: str,
) -> str:
    """Sign one immutable shadow authoring-freeze transition."""

    message = coding_authoring_freeze_signing_message(
        validator_hotkey=validator_hotkey,
        agent_id=agent_id,
        bench_version=bench_version,
        run_row_id=run_row_id,
        ticket_id=ticket_id,
        ticket_deadline=ticket_deadline,
        coding_run_id=coding_run_id,
        agent_artifact_sha256=agent_artifact_sha256,
        screened_image_sha256=screened_image_sha256,
        run_manifest_sha256=run_manifest_sha256,
        task_set_manifest_sha256=task_set_manifest_sha256,
        authoring_evidence_sha256=authoring_evidence_sha256,
        authoring_transcript_object_key=authoring_transcript_object_key,
        authoring_transcript_bytes=authoring_transcript_bytes,
        authoring_event_count=authoring_event_count,
        frozen_submission_object_key=frozen_submission_object_key,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()


def sign_coding_grading_lease(
    keypair: Any,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    run_row_id: UUID,
    ticket_id: UUID,
    freeze_id: UUID,
    authoring_evidence_sha256: str,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Sign one freeze-gated shadow grading-lease request."""

    message = coding_grading_lease_signing_message(
        validator_hotkey=validator_hotkey,
        agent_id=agent_id,
        run_row_id=run_row_id,
        ticket_id=ticket_id,
        freeze_id=freeze_id,
        authoring_evidence_sha256=authoring_evidence_sha256,
        nonce=nonce,
        requested_at=requested_at,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()


def sign_coding_shadow_result(
    keypair: Any,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    run_row_id: UUID,
    ticket_id: UUID,
    bench_version: int,
    ticket_deadline: datetime,
    agent_artifact_sha256: str,
    screened_image_sha256: str,
    run_evidence_sha256: str,
) -> str:
    """Sign one terminal shadow coding result submission."""

    message = coding_shadow_result_signing_message(
        validator_hotkey=validator_hotkey,
        agent_id=agent_id,
        run_row_id=run_row_id,
        ticket_id=ticket_id,
        bench_version=bench_version,
        ticket_deadline=ticket_deadline,
        agent_artifact_sha256=agent_artifact_sha256,
        screened_image_sha256=screened_image_sha256,
        run_evidence_sha256=run_evidence_sha256,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()


def verify_score_proof(*, agent_id: UUID, proof: LedgerScoreProof) -> bool:
    """Verify one public score receipt against its validator hotkey."""
    if not proof.signature:
        return False
    try:
        signature = bytes.fromhex(proof.signature)
        import bittensor

        verifier = bittensor.Keypair(ss58_address=proof.validator_hotkey)
        message = score_signing_message(
            validator_hotkey=proof.validator_hotkey,
            agent_id=agent_id,
            ticket_deadline=proof.ticket_deadline,
            run_id=proof.run_id,
            composite=proof.composite,
            seed=proof.seed,
            bench_version=proof.bench_version,
            transcript_sha256=proof.transcript_sha256,
            base_evidence_sha256=proof.base_evidence_sha256,
        )
        return bool(verifier.verify(message, signature))
    except (TypeError, ValueError):
        return False


def _confirmation_round_ratio(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ValueError("invalid confirmation score ratio")
    quotient, remainder = divmod(numerator, denominator)
    return quotient + int(remainder * 2 >= denominator)


def _strict_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("confirmation evidence value is not an integer")
    return value


def rebuild_v9_confirmation_evidence_root(
    job: V9ConfirmationJobResponse,
    scorer_result: V9ConfirmationScorerResult,
    prepared: V9ConfirmationPreparedReport,
) -> tuple[object, str]:
    """Independently rebuild the Platform-prepared root before signing it.

    The prepare endpoint is an untrusted transport from the validator's point
    of view.  Signing its digest directly would let a compromised Platform bind
    this validator to arbitrary normalized evidence.  Rebuilding from the
    signed lease identity, frozen execution profile, and the normalized typed
    envelopes keeps the signing key authoritative for the exact evidence it
    observed.
    """
    from ditto.api_models.validator import V9ConfirmationEvidenceRoot

    if prepared.bundle_id != job.bundle_id or prepared.ticket_id != job.ticket_id:
        raise ValueError("prepared confirmation identity does not match its lease")

    longmem_native = scorer_result.longmemeval.model_dump(mode="json")
    inference_native = scorer_result.inference_ablation.model_dump(mode="json")
    embedding_native = scorer_result.embedding_ablation.model_dump(mode="json")
    native_wire_sha256 = v9_confirmation_prepare_wire_sha256(
        ablation_coordinator_latency_ms=(scorer_result.ablation_coordinator_latency_ms),
        longmemeval=longmem_native,
        inference_ablation=inference_native,
        embedding_ablation=embedding_native,
    )
    if native_wire_sha256 != scorer_result.evidence_sha256:
        raise ValueError("scorer confirmation native wire digest mismatch")
    try:
        normalized = completion_report_from_go_dimensions(
            ablation_coordinator_latency_ms=(
                scorer_result.ablation_coordinator_latency_ms
            ),
            longmemeval=longmem_native,
            inference_ablation=inference_native,
            embedding_ablation=embedding_native,
        )
    except ConfirmationWireError as error:
        raise ValueError("scorer confirmation native evidence is invalid") from error

    latency = _strict_int(scorer_result.ablation_coordinator_latency_ms)
    if (
        prepared.ablation_coordinator_latency_ms != latency
        or prepared.longmemeval != normalized.longmemeval
        or prepared.inference_ablation != normalized.inference_ablation
        or prepared.embedding_ablation != normalized.embedding_ablation
    ):
        raise ValueError(
            "Platform-prepared confirmation envelopes do not match native evidence"
        )
    longmem = normalized.longmemeval
    inference = normalized.inference_ablation
    embedding = normalized.embedding_ablation
    totals = {
        "request_count": _strict_int(longmem.request_count),
        "input_tokens": _strict_int(longmem.input_tokens),
        "output_tokens": _strict_int(longmem.output_tokens),
        "provider_cost_microusd": _strict_int(longmem.provider_cost_microusd),
        "latency_ms": _strict_int(longmem.latency_ms) + latency,
    }
    root = V9ConfirmationEvidenceRoot.model_validate(
        {
            "schema_version": 1,
            "artifact_sha256": job.artifact_sha256,
            # Bit-for-bit paired with Platform's
            # ``confirmation_evidence.rebuild_confirmation_evidence``: the
            # digest comparison below is what makes the pair load-bearing.
            # Both sides read this off the same leased execution profile, so a
            # v9 root keeps its existing digest byte for byte and a v12 root is
            # bound to v12 instead of claiming to be a v9 run.
            "bench_version": job.execution_profile.bench_version,
            "confirmation_profile_revision": job.execution_profile.revision,
            "confirmation_profile_checksum": job.execution_profile.checksum,
            "settings_revision": job.settings_revision,
            "settings_checksum": job.settings_checksum,
            "retest_generation": job.retest_generation,
            "ablation_coordinator_latency_ms": latency,
            "composite_policy": job.execution_profile.composite.model_dump(mode="json"),
            "longmemeval": longmem,
            "inference_ablation": inference,
            "embedding_ablation": embedding,
            "totals": totals,
        }
    )
    digest = hashlib.sha256(canonical_json(root)).hexdigest()
    if digest != prepared.evidence_sha256:
        raise ValueError("Platform-prepared confirmation evidence digest mismatch")
    return root, digest


def verify_v9_confirmation_receipt(entry: LedgerEntry) -> bool:
    """Verify and replay the separate full v9 confirmation receipt.

    The ordinary score quorum proves the base root.  The bundle signature proves
    the shared confirmation evidence plus its exact composite policy.  This
    function joins those two roots and recomputes quality, gates, uncertainty,
    and the effective score without trusting Platform-carried derived scalars.
    """

    receipt = getattr(entry, "v9_confirmation", None)
    if not supports_confirmation(entry.bench_version) or receipt is None:
        return False
    try:
        root = receipt.evidence_root
        root_payload = root.model_dump(mode="json")
        evidence_sha256 = hashlib.sha256(
            json.dumps(root_payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        if evidence_sha256 != receipt.evidence_sha256:
            return False
        if root.artifact_sha256 != entry.sha256:
            return False
        message = v9_confirmation_bundle_signing_message(
            reporter_hotkey=receipt.reporter_hotkey,
            bundle_id=receipt.bundle_id,
            ticket_id=receipt.ticket_id,
            deadline=receipt.ticket_deadline,
            artifact_sha256=root.artifact_sha256,
            profile_revision=root.confirmation_profile_revision,
            profile_checksum=root.confirmation_profile_checksum,
            settings_revision=root.settings_revision,
            settings_checksum=root.settings_checksum,
            retest_generation=root.retest_generation,
            evidence_sha256=receipt.evidence_sha256,
        )
        import bittensor

        verifier = bittensor.Keypair(ss58_address=receipt.reporter_hotkey)
        if not verifier.verify(message, bytes.fromhex(receipt.bundle_signature)):
            return False

        policy = root.composite_policy
        policy_payload = {
            "schema_version": policy.schema_version,
            "revision": policy.revision,
            "formula_revision": policy.formula_revision,
            "base_weight_bps": policy.base_weight_bps,
            "longmem_weight_bps": policy.longmem_weight_bps,
        }
        policy_checksum = hashlib.sha256(
            json.dumps(
                policy_payload, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if policy_checksum != policy.checksum:
            return False

        proofs = sorted(
            entry.score_proofs,
            key=lambda proof: (proof.composite, proof.validator_hotkey),
        )
        base_proof = proofs[(len(proofs) - 1) // 2]
        base = base_proof.base_evidence
        if base is None:
            return False
        if (
            base_proof.base_evidence_sha256 != receipt.base_evidence_sha256
            or base.artifact_sha256 != root.artifact_sha256
            or base.ordinary_composite_micros != receipt.base_quality_micros
            or base.ordinary_stderr_micros != receipt.base_stderr_micros
            or base.score_gates.model_use.factor_bps != receipt.base_model_factor_bps
            or base.score_gates.authoritative_tool.factor_bps
            != receipt.base_tool_factor_bps
        ):
            return False

        if root.longmemeval.status != "completed":
            return False
        longmem_score = root.longmemeval.evidence.score
        longmem_mean = _strict_int(longmem_score.longmem_mean_micros)
        longmem_stderr = _strict_int(longmem_score.longmem_stderr_micros)
        ranking_factors = [
            receipt.base_model_factor_bps,
            receipt.base_tool_factor_bps,
        ]
        semantic_factors = list(ranking_factors)
        for envelope in (root.inference_ablation, root.embedding_ablation):
            if envelope.status != "completed":
                return False
            evidence = envelope.evidence
            if evidence.mode != "enforce":
                return False
            semantic = _strict_int(evidence.semantic_factor_bps)
            applied = _strict_int(evidence.applied_factor_bps)
            if semantic not in {0, 10_000} or applied != semantic:
                return False
            semantic_factors.append(semantic)
        semantic_factor = 0 if 0 in semantic_factors else 10_000
        ranking_factor = 0 if 0 in ranking_factors else 10_000
        quality = _confirmation_round_ratio(
            policy.base_weight_bps * receipt.base_quality_micros
            + policy.longmem_weight_bps * longmem_mean,
            10_000,
        )
        with localcontext() as context:
            context.prec = 50
            base_component = (
                Decimal(policy.base_weight_bps)
                * Decimal(receipt.base_stderr_micros)
                / Decimal(10_000)
            )
            longmem_component = (
                Decimal(policy.longmem_weight_bps)
                * Decimal(longmem_stderr)
                / Decimal(10_000)
            )
            stderr = int(
                (base_component**2 + longmem_component**2)
                .sqrt()
                .quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
        # Contract v1 cannot prove a causal ablation pass. Ranking uses the
        # 70/30 mix once both ablations finished; base model/tool still gate.
        effective = _confirmation_round_ratio(quality * ranking_factor, 10_000)
        effective_stderr = _confirmation_round_ratio(stderr * ranking_factor, 10_000)
        return (
            receipt.mode == "enforce"
            and receipt.result_status == "full_confirmed"
            and receipt.qualification_status == "qualified"
            and receipt.semantic_factor_bps == semantic_factor
            and receipt.applied_factor_bps == ranking_factor
            and receipt.full_quality_micros == quality
            and receipt.full_stderr_micros == effective_stderr
            and receipt.full_effective_micros == effective
            and entry.composite_stderr == effective_stderr / 1_000_000
        )
    except (KeyError, TypeError, ValueError):
        return False


def verify_ledger_entry(entry: LedgerEntry, *, quorum: int = 3) -> bool:
    """Verify the quorum receipts and platform-selected lower median.

    The platform is a transport/indexer here, not a score authority: every
    receipt must verify, validator hotkeys must be unique, and the ledger row
    must exactly match the deterministic lower-median receipt. This function
    answers only that cryptographic question; whether a verified score contract
    is ready for the active weight fold is a separate rollout decision.
    """
    # Historical v2-v6 ledger rows predate quorum receipts. Keep them readable
    # and weightable during the v7 fleet cutover; v7 is the first contract that
    # makes signed quorum evidence mandatory.
    if entry.bench_version is None or entry.bench_version < 7:
        return True

    proofs = entry.score_proofs
    if len(proofs) < quorum:
        return False
    if len({proof.validator_hotkey for proof in proofs}) != len(proofs):
        return False
    if any(proof.bench_version != entry.bench_version for proof in proofs):
        return False
    if any(not verify_score_proof(agent_id=entry.agent_id, proof=p) for p in proofs):
        return False

    ordered = sorted(proofs, key=lambda p: (p.composite, p.validator_hotkey))
    median = ordered[(len(ordered) - 1) // 2]
    ordinary_valid = (
        median.composite == entry.composite
        and median.validator_hotkey == entry.validator_hotkey
        and median.run_id == entry.run_id
        and median.seed == entry.seed
        and median.bench_version == entry.bench_version
        and median.signature == entry.signature
    )
    if not ordinary_valid:
        return False
    if supports_confirmation(entry.bench_version) and entry.v9_confirmation is not None:
        # The v9 receipt replaces every legacy continual-score projection. A
        # Platform must not be able to attach unsigned paired evidence and make
        # the KOTH fold bypass the verified full-confirmed composite.
        if any(
            getattr(entry, field, None)
            for field in (
                "confirmation_history",
                "confirmation_seeds",
                "confirmation_composites",
            )
        ):
            return False
        return verify_v9_confirmation_receipt(entry)
    return entry.v9_confirmation is None


def job_signing_message(
    *,
    validator_hotkey: str,
    nonce: UUID,
    requested_at: datetime,
    slot_id: str | None = None,
) -> bytes:
    """Build canonical bytes proving ownership for one job claim."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    if slot_id is None:
        return f"validator-job:{validator_hotkey}:{nonce}:{requested}".encode()
    return (
        f"validator-job:v2:{validator_hotkey}:{slot_id}:{nonce}:{requested}"
    ).encode()


def sign_job_request(
    keypair: Any,
    *,
    validator_hotkey: str,
    nonce: UUID,
    requested_at: datetime,
    slot_id: str | None = None,
) -> str:
    """Return the sr25519 signature for a fresh, one-time job claim."""
    signature: bytes = keypair.sign(
        job_signing_message(
            validator_hotkey=validator_hotkey,
            nonce=nonce,
            requested_at=requested_at,
            slot_id=slot_id,
        )
    )
    return signature.hex()


def v9_confirmation_claim_signing_message(
    *,
    validator_hotkey: str,
    slot_id: str,
    profile_revision: str,
    profile_checksum: str,
    broker_public_key: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    """Bind one internal claim to a slot and exact frozen profile."""
    if requested_at.tzinfo is None:
        raise ValueError("confirmation claim timestamp must include a timezone")
    requested = (
        requested_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation-claim:v1:"
        f"{validator_hotkey}:{slot_id}:{profile_revision}:{profile_checksum}:"
        f"{broker_public_key.rstrip('=')}:{nonce}:{requested}"
    ).encode()


def sign_v9_confirmation_claim(
    keypair: Any,
    *,
    validator_hotkey: str,
    slot_id: str,
    profile_revision: str,
    profile_checksum: str,
    broker_public_key: str,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    return keypair.sign(
        v9_confirmation_claim_signing_message(
            validator_hotkey=validator_hotkey,
            slot_id=slot_id,
            profile_revision=profile_revision,
            profile_checksum=profile_checksum,
            broker_public_key=broker_public_key,
            nonce=nonce,
            requested_at=requested_at,
        )
    ).hex()


def v9_confirmation_artifact_signing_message(
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None:
        raise ValueError("confirmation artifact timestamp must include a timezone")
    requested = (
        requested_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation-artifact:v1:"
        f"{validator_hotkey}:{bundle_id}:{ticket_id}:{nonce}:{requested}"
    ).encode()


def sign_v9_confirmation_artifact_request(
    keypair: Any,
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    return keypair.sign(
        v9_confirmation_artifact_signing_message(
            validator_hotkey=validator_hotkey,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            nonce=nonce,
            requested_at=requested_at,
        )
    ).hex()


def v9_confirmation_prepare_wire_sha256(
    *,
    ablation_coordinator_latency_ms: int,
    longmemeval: Mapping[str, object],
    inference_ablation: Mapping[str, object],
    embedding_ablation: Mapping[str, object],
) -> str:
    """Bind producer digests and measured latency without JSON float drift."""
    if (
        isinstance(ablation_coordinator_latency_ms, bool)
        or not isinstance(ablation_coordinator_latency_ms, int)
        or ablation_coordinator_latency_ms < 0
    ):
        raise ValueError("confirmation coordinator latency must be non-negative")

    def terms(name: str, dimension: Mapping[str, object]) -> str:
        digest = dimension.get("go_evidence_sha256")
        latency = dimension.get("latency_ms")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or isinstance(latency, bool)
            or not isinstance(latency, int)
            or latency < 0
        ):
            raise ValueError(f"confirmation {name} native identity is invalid")
        try:
            bytes.fromhex(digest)
        except ValueError as error:
            raise ValueError(f"confirmation {name} native digest is invalid") from error
        return f"{digest}:{latency}"

    encoded = (
        "validator-v9-confirmation-native-wire:v1:"
        f"{ablation_coordinator_latency_ms}:"
        f"{terms('longmemeval', longmemeval)}:"
        f"{terms('inference', inference_ablation)}:"
        f"{terms('embedding', embedding_ablation)}"
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def v9_confirmation_prepare_signing_message(
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    wire_sha256: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None:
        raise ValueError("confirmation prepare timestamp must include a timezone")
    requested = (
        requested_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation-prepare:v1:"
        f"{validator_hotkey}:{bundle_id}:{ticket_id}:{wire_sha256}:"
        f"{nonce}:{requested}"
    ).encode()


def sign_v9_confirmation_prepare(
    keypair: Any,
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    wire_sha256: str,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    return keypair.sign(
        v9_confirmation_prepare_signing_message(
            validator_hotkey=validator_hotkey,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            wire_sha256=wire_sha256,
            nonce=nonce,
            requested_at=requested_at,
        )
    ).hex()


def v9_confirmation_fail_signing_message(
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    reason: str,
    nonce: UUID,
    requested_at: datetime,
    failure_class: str | None = None,
    failure_stage: str | None = None,
) -> bytes:
    """Bind one hand-back, and its diagnostics when the reporter sent any.

    v1 is the original reason-only message and stays valid forever: a reporter
    predating the diagnostic contract signs it and verifies unchanged. v2 binds
    the allowlisted class and stage into the signature so a persisted diagnostic
    is exactly what the reporter signed — an unsigned advisory field could be
    forged or stripped by anything on the path, and this one is read by
    operators deciding whether to spend the next bounded canary.
    """
    if requested_at.tzinfo is None:
        raise ValueError("confirmation failure timestamp must include a timezone")
    if (failure_class is None) != (failure_stage is None):
        raise ValueError("confirmation failure class and stage must travel together")
    requested = (
        requested_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if failure_class is None:
        return (
            "validator-v9-confirmation-fail:v1:"
            f"{validator_hotkey}:{bundle_id}:{ticket_id}:{reason}:{nonce}:{requested}"
        ).encode()
    return (
        "validator-v9-confirmation-fail:v2:"
        f"{validator_hotkey}:{bundle_id}:{ticket_id}:{reason}:"
        f"{failure_class}:{failure_stage}:{nonce}:{requested}"
    ).encode()


def sign_v9_confirmation_fail(
    keypair: Any,
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    reason: str,
    nonce: UUID,
    requested_at: datetime,
    failure_class: str | None = None,
    failure_stage: str | None = None,
) -> str:
    return keypair.sign(
        v9_confirmation_fail_signing_message(
            validator_hotkey=validator_hotkey,
            failure_class=failure_class,
            failure_stage=failure_stage,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            reason=reason,
            nonce=nonce,
            requested_at=requested_at,
        )
    ).hex()


def v9_confirmation_bundle_signing_message(
    *,
    reporter_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    deadline: datetime,
    artifact_sha256: str,
    profile_revision: str,
    profile_checksum: str,
    settings_revision: int,
    settings_checksum: str,
    retest_generation: int,
    evidence_sha256: str,
) -> bytes:
    if deadline.tzinfo is None:
        raise ValueError("confirmation ticket deadline must include a timezone")
    rendered_deadline = (
        deadline.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation:v1:"
        f"{reporter_hotkey}:{bundle_id}:{ticket_id}:{rendered_deadline}:"
        f"{artifact_sha256}:9:{profile_revision}:{profile_checksum}:"
        f"{settings_revision}:{settings_checksum}:{retest_generation}:"
        f"{evidence_sha256}"
    ).encode()


def sign_v9_confirmation_bundle(
    keypair: Any,
    *,
    reporter_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    deadline: datetime,
    artifact_sha256: str,
    profile_revision: str,
    profile_checksum: str,
    settings_revision: int,
    settings_checksum: str,
    retest_generation: int,
    evidence_sha256: str,
) -> str:
    return keypair.sign(
        v9_confirmation_bundle_signing_message(
            reporter_hotkey=reporter_hotkey,
            bundle_id=bundle_id,
            ticket_id=ticket_id,
            deadline=deadline,
            artifact_sha256=artifact_sha256,
            profile_revision=profile_revision,
            profile_checksum=profile_checksum,
            settings_revision=settings_revision,
            settings_checksum=settings_checksum,
            retest_generation=retest_generation,
            evidence_sha256=evidence_sha256,
        )
    ).hex()


def inference_exchange_signing_message(
    *,
    validator_hotkey: str,
    grant_id: UUID,
    broker_public_key: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    """Bind one platform grant to one trusted broker public key."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"validator-inference:v1:{validator_hotkey}:{grant_id}:"
        f"{broker_public_key.rstrip('=')}:{nonce}:{requested}"
    ).encode()


def sign_inference_exchange(
    keypair: Any,
    *,
    validator_hotkey: str,
    grant_id: UUID,
    broker_public_key: str,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    return keypair.sign(
        inference_exchange_signing_message(
            validator_hotkey=validator_hotkey,
            grant_id=grant_id,
            broker_public_key=broker_public_key,
            nonce=nonce,
            requested_at=requested_at,
        )
    ).hex()


def job_fail_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    """Build canonical bytes proving ownership for one ticket-fail report.

    Binds the exact lease identity (``agent_id`` + ``ticket_deadline``) so a
    captured signature cannot close a different validator's ticket or a later
    reissue of this one.
    """
    deadline = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"validator-job-fail:v1:{validator_hotkey}:{agent_id}:{deadline}:"
        f"{nonce}:{requested}"
    ).encode()


def sign_job_fail_request(
    keypair: Any,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Return the sr25519 signature for a fresh ticket-fail report."""
    signature: bytes = keypair.sign(
        job_fail_signing_message(
            validator_hotkey=validator_hotkey,
            agent_id=agent_id,
            ticket_deadline=ticket_deadline,
            nonce=nonce,
            requested_at=requested_at,
        )
    )
    return signature.hex()


def top5_confirmation_job_signing_message(
    *,
    validator_hotkey: str,
    nonce: UUID,
    requested_at: datetime,
    slot_id: str | None = None,
    champion_agent_id: UUID | None = None,
    member_agent_id: UUID | None = None,
) -> bytes:
    """Build canonical bytes for one top-5 shared-seed rescore claim.

    V2 binds only the requested execution slot because Platform chooses the
    authoritative member and seed. The legacy v1 member-targeted form remains
    byte-identical for validators already in the field.
    """
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    if slot_id is not None:
        if champion_agent_id is not None or member_agent_id is not None:
            raise ValueError("slot-routed claims cannot include a legacy target")
        return (
            "validator-top5-confirmation-job:v2:"
            f"{validator_hotkey}:{slot_id}:{nonce}:{requested}"
        ).encode()
    if champion_agent_id is None or member_agent_id is None:
        raise ValueError("legacy claims require champion_agent_id and member_agent_id")
    return (
        "validator-top5-confirmation-job:v1:"
        f"{validator_hotkey}:{champion_agent_id}:{member_agent_id}:"
        f"{nonce}:{requested}"
    ).encode()


def sign_top5_confirmation_job_request(
    keypair: Any,
    *,
    validator_hotkey: str,
    nonce: UUID,
    requested_at: datetime,
    slot_id: str | None = None,
    champion_agent_id: UUID | None = None,
    member_agent_id: UUID | None = None,
) -> str:
    signature: bytes = keypair.sign(
        top5_confirmation_job_signing_message(
            validator_hotkey=validator_hotkey,
            slot_id=slot_id,
            champion_agent_id=champion_agent_id,
            member_agent_id=member_agent_id,
            nonce=nonce,
            requested_at=requested_at,
        )
    )
    return signature.hex()


def top5_confirmation_score_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime,
    run_id: str,
    bench_version: int,
    confirmation_seeds: list[int],
    confirmation_composites: list[float],
    base_evidence_sha256: str | None = None,
) -> bytes:
    """Bind every append-only seed/composite pair into one score receipt.

    Protocol 19 appends the representative v9 base-evidence digest under a v2
    domain tag. That makes its audited token counters durable authority for the
    daily retest-aware average. Absence retains the historical v1 bytes for
    non-v9 and pre-protocol-19 compatibility.
    """
    deadline = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    pairs = json.dumps(
        list(zip(confirmation_seeds, confirmation_composites, strict=True)),
        separators=(",", ":"),
    )
    version = "v2" if base_evidence_sha256 else "v1"
    message = (
        f"validator-top5-confirmation-score:{version}:"
        f"{validator_hotkey}:{agent_id}:{deadline}:{run_id}:"
        f"{bench_version}:{pairs}"
    )
    if base_evidence_sha256:
        message += f":{base_evidence_sha256}"
    return message.encode()


def sign_top5_confirmation_score(
    keypair: Any,
    **kwargs: Any,
) -> str:
    signature: bytes = keypair.sign(top5_confirmation_score_signing_message(**kwargs))
    return signature.hex()


def artifact_signing_message(
    *, validator_hotkey: str, agent_id: UUID, nonce: UUID, requested_at: datetime
) -> bytes:
    """Build canonical bytes proving ownership for one artifact request."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"validator-artifact:v1:{validator_hotkey}:{agent_id}:{nonce}:{requested}"
    ).encode()


def sign_artifact_request(
    keypair: Any,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Return the sr25519 signature for a fresh artifact request."""
    signature: bytes = keypair.sign(
        artifact_signing_message(
            validator_hotkey=validator_hotkey,
            agent_id=agent_id,
            nonce=nonce,
            requested_at=requested_at,
        )
    )
    return signature.hex()


def ledger_signing_message(
    *, validator_hotkey: str, nonce: UUID, requested_at: datetime
) -> bytes:
    """Build canonical bytes proving ownership for one ledger request."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return f"validator-ledger:v1:{validator_hotkey}:{nonce}:{requested}".encode()


def sign_ledger_request(
    keypair: Any,
    *,
    validator_hotkey: str,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Return the sr25519 signature for a fresh ledger request."""
    signature: bytes = keypair.sign(
        ledger_signing_message(
            validator_hotkey=validator_hotkey,
            nonce=nonce,
            requested_at=requested_at,
        )
    )
    return signature.hex()


def heartbeat_signing_message(
    *,
    validator_hotkey: str,
    software_version: str,
    protocol_version: int,
    code_digest: str,
    state: str,
    active_agent_id: UUID | None = None,
    system_metrics: SystemMetrics | None = None,
    benchmark_progress: BenchmarkProgress | None = None,
    capabilities: ValidatorCapabilities | None = None,
    stack: ValidatorStackIdentity | None = None,
    stack_health: ValidatorStackHealth | None = None,
    benchmark_capacity: BenchmarkCapacity | None = None,
    confirmation_progress: list[ConfirmationProgress] | None = None,
    updater_status: ValidatorUpdaterStatus | None = None,
    timestamp: int,
) -> bytes:
    """Build the canonical versioned software and runtime heartbeat payload."""
    if stack_health is not None and protocol_version < 9:
        raise ValueError("per-component stack health requires heartbeat protocol v9")
    if benchmark_capacity is not None and protocol_version < 10:
        raise ValueError("benchmark capacity requires heartbeat protocol v10+")
    if confirmation_progress is not None and protocol_version < 22:
        raise ValueError("confirmation progress requires heartbeat protocol v22")
    if updater_status is not None and protocol_version < 23:
        raise ValueError("updater status requires heartbeat protocol v23")
    if protocol_version >= 10:
        if capabilities is None or stack is None or stack_health is None:
            raise ValueError(
                "heartbeat protocol v10+ requires identity and stack health"
            )
        if capabilities.scorer_benchmarks is None:
            raise ValueError("heartbeat protocol v10+ requires scorer capabilities")
        if benchmark_capacity is None:
            raise ValueError("heartbeat protocol v10+ requires benchmark capacity")
        if protocol_version >= 22 and confirmation_progress is None:
            raise ValueError("heartbeat protocol v22 requires confirmation progress")
        if protocol_version >= 23:
            if updater_status is None:
                raise ValueError("heartbeat protocol v23 requires updater status")
            return (
                "ditto-validator-heartbeat:v23:"
                f"{validator_hotkey}:{software_version}:{protocol_version}:"
                f"{code_digest}:{state}:{active_agent_id or ''}:"
                f"{system_metrics_signing_token(system_metrics)}:"
                f"{benchmark_progress_signing_token(benchmark_progress)}:"
                f"{validator_identity_signing_token(capabilities, stack)}:"
                f"{validator_stack_health_signing_token(stack_health)}:"
                f"{benchmark_capacity_signing_token(benchmark_capacity)}:"
                f"{confirmation_progress_signing_token(confirmation_progress)}:"
                f"{validator_updater_status_signing_token(updater_status)}:"
                f"{timestamp}"
            ).encode()
        if protocol_version >= 22:
            return (
                "ditto-validator-heartbeat:v22:"
                f"{validator_hotkey}:{software_version}:{protocol_version}:"
                f"{code_digest}:{state}:{active_agent_id or ''}:"
                f"{system_metrics_signing_token(system_metrics)}:"
                f"{benchmark_progress_signing_token(benchmark_progress)}:"
                f"{validator_identity_signing_token(capabilities, stack)}:"
                f"{validator_stack_health_signing_token(stack_health)}:"
                f"{benchmark_capacity_signing_token(benchmark_capacity)}:"
                f"{confirmation_progress_signing_token(confirmation_progress)}:"
                f"{timestamp}"
            ).encode()
        signing_revision = "v11" if protocol_version >= 11 else "v10"
        return (
            f"ditto-validator-heartbeat:{signing_revision}:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{validator_identity_signing_token(capabilities, stack)}:"
            f"{validator_stack_health_signing_token(stack_health)}:"
            f"{benchmark_capacity_signing_token(benchmark_capacity)}:"
            f"{timestamp}"
        ).encode()
    if protocol_version >= 9:
        if capabilities is None or stack is None:
            raise ValueError("heartbeat protocol v9 requires capabilities and stack")
        if capabilities.scorer_benchmarks is None:
            raise ValueError("heartbeat protocol v9 requires scorer capabilities")
        if stack_health is None:
            raise ValueError("heartbeat protocol v9 requires stack health")
        identity_token = validator_identity_signing_token(capabilities, stack)
        health_token = validator_stack_health_signing_token(stack_health)
        return (
            "ditto-validator-heartbeat:v9:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{identity_token}:{health_token}:{timestamp}"
        ).encode()
    if protocol_version >= 8:
        if capabilities is None or stack is None:
            raise ValueError("heartbeat protocol v8 requires capabilities and stack")
        if capabilities.scorer_benchmarks is None:
            raise ValueError("heartbeat protocol v8 requires scorer capabilities")
        identity_token = validator_identity_signing_token(capabilities, stack)
        return (
            "ditto-validator-heartbeat:v8:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{identity_token}:{timestamp}"
        ).encode()
    if protocol_version >= 7:
        if capabilities is None or stack is None:
            raise ValueError("heartbeat protocol v7 requires capabilities and stack")
        if capabilities.scorer_benchmarks is not None:
            raise ValueError("heartbeat protocol v7 cannot claim scorer capabilities")
        identity_token = validator_identity_signing_token(capabilities, stack)
        return (
            "ditto-validator-heartbeat:v7:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{identity_token}:{timestamp}"
        ).encode()
    if protocol_version >= 4:
        return (
            "ditto-validator-heartbeat:v4:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:{timestamp}"
        ).encode()
    if protocol_version >= 3:
        return (
            "ditto-validator-heartbeat:v3:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(system_metrics)}:{timestamp}"
        ).encode()
    if protocol_version >= 2:
        return (
            "ditto-validator-heartbeat:v2:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:{timestamp}"
        ).encode()
    return (
        "ditto-validator-heartbeat:v1:"
        f"{validator_hotkey}:{software_version}:{protocol_version}:"
        f"{code_digest}:{state}:{timestamp}"
    ).encode()


def sign_heartbeat(
    keypair: Any,
    *,
    validator_hotkey: str,
    software_version: str,
    protocol_version: int,
    code_digest: str,
    state: str,
    active_agent_id: UUID | None = None,
    system_metrics: SystemMetrics | None = None,
    benchmark_progress: BenchmarkProgress | None = None,
    capabilities: ValidatorCapabilities | None = None,
    stack: ValidatorStackIdentity | None = None,
    stack_health: ValidatorStackHealth | None = None,
    benchmark_capacity: BenchmarkCapacity | None = None,
    confirmation_progress: list[ConfirmationProgress] | None = None,
    updater_status: ValidatorUpdaterStatus | None = None,
    timestamp: int,
) -> str:
    """Return the hex sr25519 signature over a software heartbeat."""
    message = heartbeat_signing_message(
        validator_hotkey=validator_hotkey,
        software_version=software_version,
        protocol_version=protocol_version,
        code_digest=code_digest,
        state=state,
        active_agent_id=active_agent_id,
        system_metrics=system_metrics,
        benchmark_progress=benchmark_progress,
        capabilities=capabilities,
        stack=stack,
        stack_health=stack_health,
        benchmark_capacity=benchmark_capacity,
        confirmation_progress=confirmation_progress,
        updater_status=updater_status,
        timestamp=timestamp,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()
