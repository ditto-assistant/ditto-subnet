"""Unit tests for the validator score signer.

Locks the canonical signing-message format, which the platform's
``_score_signing_message`` must reproduce byte-for-byte to verify.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bittensor

from ditto.validator.signing import (
    heartbeat_signing_message,
    job_signing_message,
    score_signing_message,
    sign_heartbeat,
    sign_job_request,
    sign_score,
)

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_DEADLINE = datetime(2026, 7, 9, 12, 30, tzinfo=UTC)


def test_message_is_canonical_format() -> None:
    msg = score_signing_message(
        validator_hotkey=_HOTKEY,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.82,
        seed=8675309,
    )
    assert (
        msg
        == (
            f"{_HOTKEY}:550e8400-e29b-41d4-a716-446655440000:"
            "2026-07-09T12:30:00.000000+00:00:run_1:0.82:8675309"
        ).encode()
    )


def test_sign_verifies_with_real_keypair() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    sig_hex = sign_score(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.82,
        seed=42,
    )
    msg = score_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.82,
        seed=42,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert verifier.verify(msg, bytes.fromhex(sig_hex))


def test_tampered_composite_breaks_signature() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    sig_hex = sign_score(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.50,
        seed=42,
    )
    # Verifying against a different composite must fail.
    tampered = score_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.99,
        seed=42,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert not verifier.verify(tampered, bytes.fromhex(sig_hex))


def test_superseded_ticket_deadline_breaks_signature() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    sig_hex = sign_score(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE,
        run_id="run_1",
        composite=0.50,
        seed=42,
    )
    superseded = score_signing_message(
        validator_hotkey=keypair.ss58_address,
        agent_id=_AGENT,
        ticket_deadline=_DEADLINE + timedelta(minutes=30),
        run_id="run_1",
        composite=0.50,
        seed=42,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert not verifier.verify(superseded, bytes.fromhex(sig_hex))


def test_job_claim_signature_binds_hotkey_nonce_and_timestamp() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    nonce = UUID("7f4d1800-4cf1-4a24-8fd5-2e4cd59942ae")
    requested_at = datetime(2026, 7, 14, 1, 30, tzinfo=UTC)
    signature = sign_job_request(
        keypair,
        validator_hotkey=keypair.ss58_address,
        nonce=nonce,
        requested_at=requested_at,
    )
    message = job_signing_message(
        validator_hotkey=keypair.ss58_address,
        nonce=nonce,
        requested_at=requested_at,
    )
    assert keypair.verify(message, bytes.fromhex(signature))
    replay_as_other_nonce = job_signing_message(
        validator_hotkey=keypair.ss58_address,
        nonce=UUID("e879178e-baf4-41f0-9467-9da18b65ac17"),
        requested_at=requested_at,
    )
    assert not keypair.verify(replay_as_other_nonce, bytes.fromhex(signature))


def test_heartbeat_signature_binds_build_state_and_timestamp() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    signature = sign_heartbeat(
        keypair,
        validator_hotkey=keypair.ss58_address,
        software_version="0.1.0",
        protocol_version=2,
        code_digest="ab" * 32,
        state="running_benchmark",
        active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
        timestamp=1_752_443_200,
    )
    verifier = bittensor.Keypair(ss58_address=keypair.ss58_address)
    assert verifier.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.1.0",
            protocol_version=2,
            code_digest="ab" * 32,
            state="running_benchmark",
            active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )
    assert not verifier.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.1.0",
            protocol_version=2,
            code_digest="cd" * 32,
            state="running_benchmark",
            active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )
    assert not verifier.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.1.0",
            protocol_version=2,
            code_digest="ab" * 32,
            state="idle",
            active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )
    assert not verifier.verify(
        heartbeat_signing_message(
            validator_hotkey=keypair.ss58_address,
            software_version="0.1.0",
            protocol_version=2,
            code_digest="ab" * 32,
            state="running_benchmark",
            active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
            timestamp=1_752_443_200,
        ),
        bytes.fromhex(signature),
    )
