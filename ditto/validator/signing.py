"""Validator hotkey loading + score signing.

The worker signs each score submission so the platform can verify the report
came from the claimed validator hotkey *and* that its contents were not tampered
with. The signature binds a **canonical payload** — the validator hotkey, the
agent id, and the reported ``run_id`` / ``composite`` / ``seed`` — so a captured
signature cannot be replayed against a different agent, and the composite the
platform records cannot be altered without invalidating the signature. (The
platform's ``/validator/.../score`` rebuilds the same string and verifies it.)

The signing private key comes from a bittensor wallet on the host. We only hold
the public hotkey (``5CZq6Mdanx...``) in config; the secret half must be
provisioned on the VM as a wallet file (Secret Manager -> wallet file). Never log
the key.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
from ditto.validator.errors import ValidatorConfigError

if TYPE_CHECKING:
    from ditto.api_models.validator import LedgerEntry, LedgerScoreProof
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
) -> bytes:
    """Build the canonical bytes a score signature is computed over.

    ``{validator_hotkey}:{agent_id}:{ticket_deadline}:{run_id}:``
    ``{composite!r}:{seed}`` — then optional ``:{bench_version}``, followed by
    optional ``:{transcript_sha256}`` when the report declares a transcript
    digest (``details["transcript_sha256"]``), so
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
    if transcript_sha256:
        message += f":{transcript_sha256}"
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
        )
        return bool(verifier.verify(message, signature))
    except (TypeError, ValueError):
        return False


def verify_ledger_entry(entry: LedgerEntry, *, quorum: int = 3) -> bool:
    """Verify the quorum receipts and platform-selected lower median.

    The platform is a transport/indexer here, not a score authority: every
    receipt must verify, validator hotkeys must be unique, and the ledger row
    must exactly match the deterministic lower-median receipt.
    """
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
    return (
        median.composite == entry.composite
        and median.validator_hotkey == entry.validator_hotkey
        and median.run_id == entry.run_id
        and median.seed == entry.seed
        and median.bench_version == entry.bench_version
        and median.signature == entry.signature
    )


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
    champion_agent_id: UUID,
    member_agent_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    """Build canonical bytes for one top-5 shared-seed rescore claim.

    Distinct domain tag (``validator-top5-confirmation-job:v1``) from the
    single-leader confirmation claim, so a signature for one lane can never be
    replayed into the other.
    """
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        "validator-top5-confirmation-job:v1:"
        f"{validator_hotkey}:{champion_agent_id}:{member_agent_id}:"
        f"{nonce}:{requested}"
    ).encode()


def sign_top5_confirmation_job_request(
    keypair: Any,
    *,
    validator_hotkey: str,
    champion_agent_id: UUID,
    member_agent_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    signature: bytes = keypair.sign(
        top5_confirmation_job_signing_message(
            validator_hotkey=validator_hotkey,
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
) -> bytes:
    """Bind every append-only seed/composite pair into one score receipt."""
    deadline = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    pairs = json.dumps(
        list(zip(confirmation_seeds, confirmation_composites, strict=True)),
        separators=(",", ":"),
    )
    return (
        "validator-top5-confirmation-score:v1:"
        f"{validator_hotkey}:{agent_id}:{deadline}:{run_id}:"
        f"{bench_version}:{pairs}"
    ).encode()


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
    timestamp: int,
) -> bytes:
    """Build the canonical versioned software and runtime heartbeat payload."""
    if stack_health is not None and protocol_version < 9:
        raise ValueError("per-component stack health requires heartbeat protocol v9")
    if benchmark_capacity is not None and protocol_version < 10:
        raise ValueError("benchmark capacity requires heartbeat protocol v10+")
    if protocol_version >= 10:
        if capabilities is None or stack is None or stack_health is None:
            raise ValueError(
                "heartbeat protocol v10+ requires identity and stack health"
            )
        if capabilities.scorer_benchmarks is None:
            raise ValueError("heartbeat protocol v10+ requires scorer capabilities")
        if benchmark_capacity is None:
            raise ValueError("heartbeat protocol v10+ requires benchmark capacity")
        signing_revision = "v11" if protocol_version >= 11 else "v10"
        return (
            f"ditto-validator-heartbeat:{signing_revision}:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{validator_identity_signing_token(capabilities, stack)}:"
            f"{validator_stack_health_signing_token(stack_health)}:"
            f"{benchmark_capacity_signing_token(benchmark_capacity)}:{timestamp}"
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
        timestamp=timestamp,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()
