"""Node-2 replay keys are operator pinned while capacity stays zero."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException
from sqlalchemy import select

from ditto.api_server.endpoints import admin_screener_capacity, verification_replay
from ditto.api_server.endpoints.verification_replay import (
    ReplayProcessKeyRevoke,
    ReplayProcessKeyWrite,
    get_replay_process_readiness,
    register_replay_process_key,
    revoke_replay_process_key,
    verify_replay_process_request,
)
from ditto.db.models import (
    ScreenerHeartbeat,
    ScreenerNode,
    ScreenerReplayProcessKey,
    ScreenerReplayProcessNonce,
)
from ditto_screening_protocol.v13_replay_process_identity import V13ReplayProcessProof

NODE = "subnet-screener-2"
HOTKEY = "5IndependentReplayHotkey"
INSTANCE = f"{NODE}-worker-1"


async def _node(session, *, capacity=0):
    node = ScreenerNode(
        environment="prod",
        node_id=NODE,
        provider="hetzner",
        provider_resource_id="separate-physical-host",
        screener_hotkey=HOTKEY,
        token_hash="a" * 64,
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        status="active",
        verification_replay_capacity=capacity,
    )
    session.add(node)
    await session.commit()
    return node


def _key():
    private = Ed25519PrivateKey.generate()
    public_hex = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return private, public_hex, hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()


def _write(public_hex, key_sha):
    return ReplayProcessKeyWrite(
        expected_hotkey=HOTKEY,
        instance_id=INSTANCE,
        public_key_hex=public_hex,
        reason="Canary-only independent process registration",
        confirmation=f"REGISTER V13 REPLAY PROCESS {NODE}/{INSTANCE}/{key_sha}",
    )


@pytest.mark.asyncio
async def test_operator_registration_and_exact_revocation(session):
    await _node(session)
    _, public_hex, key_sha = _key()
    await register_replay_process_key(
        NODE, _write(public_hex, key_sha), None, session, "operator-test"
    )
    row = await session.get(ScreenerReplayProcessKey, key_sha)
    assert row is not None and row.status == "active" and row.revision == 1
    await session.commit()
    with pytest.raises(HTTPException) as duplicate:
        await register_replay_process_key(
            NODE, _write(public_hex, key_sha), None, session, "operator-test"
        )
    assert duplicate.value.status_code == 409
    await session.rollback()
    await revoke_replay_process_key(
        NODE,
        ReplayProcessKeyRevoke(
            expected_hotkey=HOTKEY,
            expected_key_sha256=key_sha,
            reason="Revoke canary process key after test",
            confirmation=f"REVOKE V13 REPLAY PROCESS {NODE}/{key_sha}",
        ),
        None,
        session,
        "operator-test",
    )
    await session.refresh(row)
    assert row.status == "revoked" and row.revoked_at is not None
    await session.commit()
    with pytest.raises(HTTPException) as reused:
        await register_replay_process_key(
            NODE, _write(public_hex, key_sha), None, session, "operator-test"
        )
    assert reused.value.status_code == 409


@pytest.mark.asyncio
async def test_registration_refuses_nonzero_capacity(session):
    await _node(session, capacity=1)
    _, public_hex, key_sha = _key()
    with pytest.raises(HTTPException) as blocked:
        await register_replay_process_key(
            NODE, _write(public_hex, key_sha), None, session, "operator-test"
        )
    assert blocked.value.status_code == 409


@pytest.mark.asyncio
async def test_emergency_key_revocation_remains_available_at_capacity_one(session):
    node = await _node(session)
    _, public_hex, key_sha = _key()
    await register_replay_process_key(
        NODE, _write(public_hex, key_sha), None, session, "operator-test"
    )
    node.verification_replay_capacity = 1
    await session.commit()
    await revoke_replay_process_key(
        NODE,
        ReplayProcessKeyRevoke(
            expected_hotkey=HOTKEY,
            expected_key_sha256=key_sha,
            reason="Emergency revoke after stale process",
            confirmation=f"REVOKE V13 REPLAY PROCESS {NODE}/{key_sha}",
        ),
        None,
        session,
        "operator-test",
    )
    await session.commit()
    key = await session.get(ScreenerReplayProcessKey, key_sha)
    assert key is not None and key.status == "revoked"


@pytest.mark.asyncio
async def test_readiness_requires_exact_fresh_signed_worker_and_release(
    session, monkeypatch
):
    await _node(session)
    empty = await get_replay_process_readiness(NODE, None, session)
    assert not empty.ready_for_capacity_one
    assert "active_process_key" in empty.missing
    await session.commit()
    _, public_hex, key_sha = _key()
    await register_replay_process_key(
        NODE, _write(public_hex, key_sha), None, session, "operator-test"
    )
    now = datetime.now(UTC)
    session.add(
        ScreenerHeartbeat(
            screener_hotkey=HOTKEY,
            instance_id=INSTANCE,
            software_version="0.21.2",
            protocol_version=7,
            policy_version=13,
            state="polling",
            system_metrics={
                "replay_process": {"key_sha256": key_sha},
                "release": {
                    "builtin_policy_version": 13,
                    "revision": "a" * 40,
                    "version": "0.301.0",
                    "activated_at": int(now.timestamp()) - 60,
                },
            },
            reported_at=now,
            seen_at=now,
            signature="a" * 128,
        )
    )
    await session.commit()
    monkeypatch.setattr(
        verification_replay, "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE", (0, 301, 0)
    )
    monkeypatch.setattr(
        admin_screener_capacity, "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE", (0, 301, 0)
    )
    ready = await get_replay_process_readiness(NODE, None, session)
    assert ready.ready_for_capacity_one and ready.active_key_sha256 == key_sha
    assert ready.signed_heartbeat_fresh and ready.release_qualified
    session.add(
        ScreenerHeartbeat(
            screener_hotkey=HOTKEY,
            instance_id=f"{NODE}-worker-2",
            software_version="0.21.2",
            protocol_version=7,
            policy_version=13,
            state="polling",
            system_metrics={},
            reported_at=now,
            seen_at=now,
            signature="b" * 128,
        )
    )
    await session.commit()
    sibling = await get_replay_process_readiness(NODE, None, session)
    assert not sibling.ready_for_capacity_one
    assert "capacity_admission_gate" in sibling.missing
    sibling_row = await session.get(ScreenerHeartbeat, (HOTKEY, f"{NODE}-worker-2"))
    assert sibling_row is not None
    sibling_row.seen_at = now - timedelta(minutes=6)
    await session.commit()
    heartbeat = await session.get(ScreenerHeartbeat, (HOTKEY, INSTANCE))
    assert heartbeat is not None
    heartbeat.seen_at = now - timedelta(minutes=6)
    await session.commit()
    stale = await get_replay_process_readiness(NODE, None, session)
    assert not stale.ready_for_capacity_one
    assert "signed_worker_heartbeat_current" in stale.missing


class _Request:
    def __init__(self, proof, signature):
        self.headers = {
            "x-replay-process-proof": proof.model_dump_json(),
            "x-replay-process-signature": signature,
        }

    async def body(self):
        return b""


@pytest.mark.asyncio
async def test_signed_claim_requires_exact_heartbeat_and_once_only_nonce(
    session, monkeypatch
):
    node = await _node(session)
    private, public_hex, key_sha = _key()
    await register_replay_process_key(
        NODE, _write(public_hex, key_sha), None, session, "operator-test"
    )
    now = datetime.now(UTC)
    session.add(
        ScreenerHeartbeat(
            screener_hotkey=HOTKEY,
            instance_id=INSTANCE,
            software_version="0.21.2",
            protocol_version=7,
            policy_version=13,
            state="polling",
            system_metrics={
                "replay_process": {"key_sha256": key_sha},
                "release": {
                    "builtin_policy_version": 13,
                    "revision": "a" * 40,
                    "version": "0.301.0",
                    "activated_at": int(now.timestamp()) - 60,
                },
            },
            reported_at=now,
            seen_at=now,
            signature="a" * 128,
        )
    )
    await session.commit()
    monkeypatch.setattr(
        verification_replay, "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE", (0, 301, 0)
    )
    proof = V13ReplayProcessProof(
        purpose="claim",
        node_id=NODE,
        instance_id=INSTANCE,
        path="/screener/verification-replays/claim",
        body_sha256=hashlib.sha256(b"").hexdigest(),
        issued_at=int(now.timestamp()),
        nonce="a" * 32,
    )
    request = _Request(proof, private.sign(proof.signing_bytes()).hex())
    assert (
        await verify_replay_process_request(
            request, session, node=node, instance_id=INSTANCE, purpose="claim"
        )
        == key_sha
    )
    await session.commit()
    used = await session.scalar(select(ScreenerReplayProcessNonce))
    assert used is not None and used.key_sha256 == key_sha
    session.expunge(used)
    with pytest.raises(HTTPException, match="replayed"):
        await verify_replay_process_request(
            request, session, node=node, instance_id=INSTANCE, purpose="claim"
        )


@pytest.mark.asyncio
async def test_signed_claim_rejects_healthy_sibling_heartbeat(session, monkeypatch):
    node = await _node(session)
    private, public_hex, key_sha = _key()
    await register_replay_process_key(
        NODE, _write(public_hex, key_sha), None, session, "operator-test"
    )
    now = datetime.now(UTC)
    session.add(
        ScreenerHeartbeat(
            screener_hotkey=HOTKEY,
            instance_id=f"{NODE}-worker-2",
            software_version="0.21.2",
            protocol_version=7,
            policy_version=13,
            state="polling",
            system_metrics={
                "replay_process": {"key_sha256": key_sha},
                "release": {
                    "builtin_policy_version": 13,
                    "revision": "a" * 40,
                    "version": "0.301.0",
                    "activated_at": int(now.timestamp()) - 60,
                },
            },
            reported_at=now,
            seen_at=now,
            signature="a" * 128,
        )
    )
    await session.commit()
    monkeypatch.setattr(
        verification_replay, "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE", (0, 301, 0)
    )
    proof = V13ReplayProcessProof(
        purpose="claim",
        node_id=NODE,
        instance_id=INSTANCE,
        path="/screener/verification-replays/claim",
        body_sha256=hashlib.sha256(b"").hexdigest(),
        issued_at=int(now.timestamp()),
        nonce="b" * 32,
    )
    request = _Request(proof, private.sign(proof.signing_bytes()).hex())
    with pytest.raises(HTTPException, match="heartbeat"):
        await verify_replay_process_request(
            request, session, node=node, instance_id=INSTANCE, purpose="claim"
        )
