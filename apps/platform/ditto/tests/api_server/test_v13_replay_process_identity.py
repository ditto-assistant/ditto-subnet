"""Report-only replay requires the exact registered process, not its sibling."""

import hashlib
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ditto.api_server.v13_replay_process_identity import (
    ReplayProcessProofError,
    ReplayProcessRegistration,
    VerifiedReplayProcessHeartbeat,
    verify_replay_process_proof,
)
from ditto_screening_protocol.v13_replay_process_identity import V13ReplayProcessProof

NOW = 1_790_200_000
NODE = "subnet-screener-2"
INSTANCE = f"{NODE}-worker-1"


def _fixture():
    key = Ed25519PrivateKey.generate()
    public_hex = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    registration = ReplayProcessRegistration(NODE, INSTANCE, public_hex)
    heartbeat = VerifiedReplayProcessHeartbeat(
        NODE, INSTANCE, registration.key_sha256, NOW, 13, (0, 301, 0)
    )
    proof = V13ReplayProcessProof(
        purpose="claim",
        node_id=NODE,
        instance_id=INSTANCE,
        path="/screener/verification-replays/claim",
        body_sha256=hashlib.sha256(b"").hexdigest(),
        issued_at=NOW,
        nonce="a" * 32,
    )
    return key, registration, heartbeat, proof


async def _verify(
    *,
    key=None,
    registration=None,
    heartbeat=None,
    proof=None,
    body=b"",
    purpose="claim",
    method="POST",
    path=None,
    authenticated_node_id=NODE,
    now=NOW,
    minimum_release=(0, 301, 0),
    used=None,
):
    good_key, good_registration, good_heartbeat, good_proof = _fixture()
    key = key or good_key
    registration = registration or good_registration
    heartbeat = good_heartbeat if heartbeat is None else heartbeat
    proof = proof or good_proof
    used = set() if used is None else used

    async def consume_nonce(node_id, instance_id, nonce):
        identity = (node_id, instance_id, nonce)
        if identity in used:
            return False
        used.add(identity)
        return True

    await verify_replay_process_proof(
        proof=proof,
        signature_hex=key.sign(proof.signing_bytes()).hex(),
        registration=registration,
        authenticated_node_id=authenticated_node_id,
        purpose=purpose,
        body=body,
        method=method,
        path=path,
        now=now,
        consume_nonce=consume_nonce,
        heartbeat=heartbeat,
        minimum_release=minimum_release,
    )
    return used


@pytest.mark.asyncio
async def test_exact_process_claim_verifies() -> None:
    key, registration, heartbeat, proof = _fixture()
    await _verify(key=key, registration=registration, heartbeat=heartbeat, proof=proof)


@pytest.mark.asyncio
async def test_healthy_sibling_cannot_qualify_stale_claimant() -> None:
    key, registration, heartbeat, proof = _fixture()
    sibling = replace(
        heartbeat,
        instance_id=f"{NODE}-worker-2",
        seen_at=NOW,
    )
    with pytest.raises(ReplayProcessProofError, match="exact.*heartbeat"):
        await _verify(
            key=key, registration=registration, heartbeat=sibling, proof=proof
        )


@pytest.mark.asyncio
async def test_revoked_key_and_wrong_node_fail() -> None:
    key, registration, heartbeat, proof = _fixture()
    with pytest.raises(ReplayProcessProofError, match="revoked"):
        await _verify(
            key=key,
            registration=replace(registration, revoked=True),
            heartbeat=heartbeat,
            proof=proof,
        )
    with pytest.raises(ReplayProcessProofError, match="binding"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            authenticated_node_id="subnet-screener-1",
        )


@pytest.mark.asyncio
async def test_nonce_replay_fails() -> None:
    key, registration, heartbeat, proof = _fixture()
    used = await _verify(
        key=key, registration=registration, heartbeat=heartbeat, proof=proof
    )
    with pytest.raises(ReplayProcessProofError, match="replayed"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            used=used,
        )


@pytest.mark.asyncio
async def test_clock_skew_and_stale_heartbeat_fail() -> None:
    key, registration, heartbeat, proof = _fixture()
    with pytest.raises(ReplayProcessProofError, match="time window"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            now=NOW + 31,
        )
    with pytest.raises(ReplayProcessProofError, match="time window"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            now=NOW - 6,
        )
    with pytest.raises(ReplayProcessProofError, match="heartbeat"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=replace(heartbeat, seen_at=NOW - 301),
            proof=proof,
        )


@pytest.mark.asyncio
async def test_wrong_body_purpose_and_release_fail() -> None:
    key, registration, heartbeat, proof = _fixture()
    with pytest.raises(ReplayProcessProofError, match="binding"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            body=b"different",
        )
    with pytest.raises(ReplayProcessProofError, match="binding"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            purpose="heartbeat",
        )
    with pytest.raises(ReplayProcessProofError, match="heartbeat"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            minimum_release=(0, 302, 0),
        )


@pytest.mark.asyncio
async def test_wrong_signature_fails() -> None:
    key, registration, heartbeat, proof = _fixture()
    with pytest.raises(ReplayProcessProofError, match="signature"):
        await _verify(
            key=Ed25519PrivateKey.generate(),
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
        )


@pytest.mark.asyncio
async def test_exact_lease_route_cannot_be_retargeted() -> None:
    key, registration, heartbeat, _ = _fixture()
    path = "/screener/verification-replays/123e4567-e89b-42d3-a456-426614174000/inputs"
    proof = V13ReplayProcessProof(
        purpose="inputs",
        node_id=NODE,
        instance_id=INSTANCE,
        method="GET",
        path=path,
        body_sha256=hashlib.sha256(b"").hexdigest(),
        issued_at=NOW,
        nonce="b" * 32,
    )
    await _verify(
        key=key,
        registration=registration,
        heartbeat=heartbeat,
        proof=proof,
        purpose="inputs",
        method="GET",
        path=path,
    )
    with pytest.raises(ReplayProcessProofError, match="binding"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            purpose="inputs",
            method="GET",
            path=path.replace("4000", "4001"),
        )
    with pytest.raises(ReplayProcessProofError, match="binding"):
        await _verify(
            key=key,
            registration=registration,
            heartbeat=heartbeat,
            proof=proof,
            purpose="inputs",
            method="POST",
            path=path,
        )
