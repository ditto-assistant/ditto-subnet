"""The replay process signs exact HTTP bytes under a separate private key."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ditto_screener.errors import PlatformError
from ditto_screener.heartbeat import ScreenerHeartbeatRequest
from ditto_screener.platform import PlatformClient
from ditto_screener.replay_api import ReplayApiClient
from ditto_screener.replay_process import ReplayProcessIdentity
from ditto_screening_protocol.v13_replay_process_identity import V13ReplayProcessProof


def _identity(tmp_path: Path) -> tuple[ReplayProcessIdentity, Path]:
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "process.seed"
    path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o400)
    return (
        ReplayProcessIdentity(
            node_id="subnet-screener-2",
            instance_id="subnet-screener-2-worker-1",
            key_file=path,
        ),
        path,
    )


def test_process_proof_binds_exact_request_and_unique_nonce(tmp_path: Path) -> None:
    identity, _ = _identity(tmp_path)
    body = b'{"artifact_sha256":"' + b"a" * 64 + b'"}'
    path = "/screener/verification-replays/claim"
    first = identity.headers(purpose="claim", path=path, body=body, now=1000)
    second = identity.headers(purpose="claim", path=path, body=body, now=1000)
    proof = V13ReplayProcessProof.model_validate_json(first["X-Replay-Process-Proof"])
    assert proof.matches_request(
        purpose="claim",
        node_id="subnet-screener-2",
        instance_id="subnet-screener-2-worker-1",
        body_sha256=hashlib.sha256(body).hexdigest(),
        path=path,
    )
    assert not proof.matches_request(
        purpose="claim",
        node_id="subnet-screener-2",
        instance_id="subnet-screener-2-worker-1",
        body_sha256=hashlib.sha256(body + b" ").hexdigest(),
        path=path,
    )
    assert (
        json.loads(first["X-Replay-Process-Proof"])["nonce"]
        != json.loads(second["X-Replay-Process-Proof"])["nonce"]
    )
    public = Ed25519PrivateKey.from_private_bytes(
        (tmp_path / "process.seed").read_bytes()
    ).public_key()
    public.verify(
        bytes.fromhex(first["X-Replay-Process-Signature"]),
        proof.signing_bytes(),
    )


def test_process_key_refuses_world_readable_file_or_symlink(tmp_path: Path) -> None:
    _, path = _identity(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        ReplayProcessIdentity(
            node_id="subnet-screener-2",
            instance_id="subnet-screener-2-worker-1",
            key_file=path,
        )
    path.chmod(0o400)
    link = tmp_path / "link.seed"
    link.symlink_to(path)
    with pytest.raises(OSError):
        ReplayProcessIdentity(
            node_id="subnet-screener-2",
            instance_id="subnet-screener-2-worker-1",
            key_file=link,
        )


async def test_replay_api_signs_exact_bytes_once(tmp_path: Path) -> None:
    identity, _ = _identity(tmp_path)
    seen: list[httpx.Request] = []
    replay_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        proof = V13ReplayProcessProof.model_validate_json(
            request.headers["X-Replay-Process-Proof"]
        )
        assert proof.path == request.url.path.removeprefix("/api/v1")
        assert proof.body_sha256 == hashlib.sha256(request.content).hexdigest()
        assert request.headers["authorization"] == "Bearer node-token"
        if proof.purpose == "claim":
            return httpx.Response(200, content=b"null")
        if proof.purpose == "heartbeat":
            assert request.url.path == "/api/v1/screener/heartbeat"
            assert json.loads(request.content)["instance_id"] == (
                "subnet-screener-2-worker-1"
            )
            return httpx.Response(
                200, json={"accepted": True, "seen_at": datetime.now(UTC).isoformat()}
            )
        if proof.purpose == "inputs":
            assert request.method == "GET" and request.content == b""
            return httpx.Response(200, json={"artifact_url": "https://example.test/a"})
        if proof.purpose == "finish":
            assert request.url.path.endswith(f"/{replay_id}/finish")
            assert json.loads(request.content) == {
                "artifact_sha256": "a" * 64,
                "failure_code": "runner-unavailable",
                "image_sha256": None,
                "status": "failed",
            }
            return httpx.Response(200, json={"status": "failed"})
        return httpx.Response(503, json={"detail": "unavailable"})

    async def auth_headers() -> dict[str, str]:
        return {"Authorization": "Bearer node-token"}

    platform = cast(
        PlatformClient,
        SimpleNamespace(
            _base="https://platform.example",
            _auth_headers=auth_headers,
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        api = ReplayApiClient(platform, http, identity)
        assert await api.claim() is None
        heartbeat = ScreenerHeartbeatRequest(
            screener_hotkey="1" * 47,
            software_version="0.21.2",
            protocol_version=3,
            policy_version=13,
            state="polling",
            instance_id="subnet-screener-2-worker-1",
            timestamp=1000,
            signature="0" * 128,
        )
        assert (await api.heartbeat(heartbeat)).accepted
        assert (await api.inputs(replay_id))["artifact_url"] == (
            "https://example.test/a"
        )
        with pytest.raises(PlatformError, match="HTTP 503"):
            await api.receipt(
                uuid4(),
                cast(dict[str, Any], {"artifact_sha256": "a" * 64}),
            )
        assert (
            await api.finish(
                replay_id,
                status="failed",
                artifact_sha256="a" * 64,
                failure_code="runner-unavailable",
            )
        )["status"] == "failed"
    assert len(seen) == 5
    assert seen[0].method == "POST" and seen[0].content == b""
    assert seen[1].headers["content-type"] == "application/json"
