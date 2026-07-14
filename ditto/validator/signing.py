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

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ditto.validator.errors import ValidatorConfigError

if TYPE_CHECKING:
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
) -> bytes:
    """Build the canonical bytes a score signature is computed over.

    ``{validator_hotkey}:{agent_id}:{ticket_deadline}:{run_id}:``
    ``{composite!r}:{seed}``. The exact ticket deadline is the lease identity;
    platform reconstructs this exact string from the request to verify, so both
    sides MUST format it identically — in particular ``composite`` uses Python's
    shortest round-trip float repr, which the JSON transport preserves.
    """
    lease = (
        ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
        if ticket_deadline is not None
        else ""
    )
    return (
        f"{validator_hotkey}:{agent_id}:{lease}:{run_id}:{composite!r}:{seed}"
    ).encode()


def sign_score(
    keypair: Any,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime | None = None,
    run_id: str,
    composite: float,
    seed: int,
) -> str:
    """Return the hex sr25519 signature over the canonical score payload."""
    message = score_signing_message(
        validator_hotkey=validator_hotkey,
        agent_id=agent_id,
        ticket_deadline=ticket_deadline,
        run_id=run_id,
        composite=composite,
        seed=seed,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()


def job_signing_message(
    *, validator_hotkey: str, nonce: UUID, requested_at: datetime
) -> bytes:
    """Build canonical bytes proving ownership for one job claim."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return f"validator-job:{validator_hotkey}:{nonce}:{requested}".encode()


def sign_job_request(
    keypair: Any,
    *,
    validator_hotkey: str,
    nonce: UUID,
    requested_at: datetime,
) -> str:
    """Return the sr25519 signature for a fresh, one-time job claim."""
    signature: bytes = keypair.sign(
        job_signing_message(
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
    timestamp: int,
) -> bytes:
    """Build the canonical v1 software and runtime heartbeat payload."""
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
    timestamp: int,
) -> str:
    """Return the hex sr25519 signature over a software heartbeat."""
    message = heartbeat_signing_message(
        validator_hotkey=validator_hotkey,
        software_version=software_version,
        protocol_version=protocol_version,
        code_digest=code_digest,
        state=state,
        timestamp=timestamp,
    )
    signature: bytes = keypair.sign(message)
    return signature.hex()
