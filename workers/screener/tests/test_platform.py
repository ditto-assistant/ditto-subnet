"""Tests for the screener platform HTTP client (mocked transport)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from ditto_screener.config import ScreenerConfig
from ditto_screener.enrollment import (
    NodeCredential,
    load_node_credential,
    store_node_credential,
)
from ditto_screener.errors import PlatformError
from ditto_screener.heartbeat import ScreenerHeartbeatRequest
from ditto_screener.platform import (
    _REMOTE_SOURCE_REVIEW_SETTLEMENT_GRACE_SECONDS,
    PlatformClient,
    RemoteSubmissionBuildRejected,
    _remote_source_review_poll_deadline,
)
from ditto_screener.review_settings import bootstrap_review_settings
from ditto_screening_protocol import SCREENING_POLICY_VERSION, ScreenResultOutcome

_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_TOKEN = "test-screener-token-at-least-32-characters"


def test_remote_source_review_poll_reserves_terminal_commit_grace() -> None:
    assert _remote_source_review_poll_deadline(now=10.0, timeout=1_800.0) == (
        10.0 + 1_800.0 + _REMOTE_SOURCE_REVIEW_SETTLEMENT_GRACE_SECONDS
    )


def _assert_auth(request: httpx.Request) -> None:
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert request.headers["X-Screener-Hotkey"]


def _make_client(
    cfg: ScreenerConfig, handler: Callable[[httpx.Request], httpx.Response]
) -> tuple[PlatformClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PlatformClient(cfg, http), http


async def test_claim_next_parses_leased_item(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    review_settings = bootstrap_review_settings(make_config())
    checksum = review_settings.checksum

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/screener/claim"
        assert request.url.params["policy_version"] == str(SCREENING_POLICY_VERSION)
        assert request.url.params["canary_policy_version"] == str(
            SCREENING_POLICY_VERSION
        )
        assert request.url.params["renewable_lease"] == "true"
        assert request.url.params["review_settings_revision"] == "0"
        assert request.url.params["review_settings_instance_id"] == "worker-1"
        assert request.url.params["review_settings_scope"] == review_settings.scope
        assert request.url.params["review_settings_checksum"] == checksum
        _assert_auth(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "agent_id": str(_AGENT),
                        "bench_version": 12,
                        "miner_hotkey": _MINER,
                        "name": "alpha",
                        "sha256": "de" * 32,
                        "status": "screening",
                        "created_at": "2026-07-06T12:00:00Z",
                        "attempt_id": "550e8400-e29b-41d4-a716-446655440001",
                        "lease_deadline": "2026-07-06T12:30:00Z",
                        "policy_version": SCREENING_POLICY_VERSION,
                    }
                ],
                "count": 1,
                "required_policy_version": SCREENING_POLICY_VERSION,
            },
        )

    client, http = _make_client(make_config(), handler)
    async with http:
        resp = await client.claim_next(
            policy_version=SCREENING_POLICY_VERSION,
            review_settings=review_settings,
            instance_id="worker-1",
        )
    assert resp.count == 1
    assert resp.items[0].agent_id == _AGENT
    assert resp.items[0].bench_version == 12
    assert resp.items[0].sha256 == "de" * 32


async def test_policy_preflight_is_read_only(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/screener/queue"
        _assert_auth(request)
        return httpx.Response(
            200,
            json={
                "items": [],
                "count": 0,
                "required_policy_version": SCREENING_POLICY_VERSION,
            },
        )

    client, http = _make_client(make_config(), handler)
    async with http:
        required = await client.get_required_policy_version()
    assert required == SCREENING_POLICY_VERSION


async def test_enrolled_node_refresh_failure_is_single_shot(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    credential_file = tmp_path / "node.json"
    old_token = "old-node-token-at-least-43-characters-xxxxxxxx"
    new_token = "new-node-token-at-least-43-characters-xxxxxxxx"
    credential = NodeCredential(
        environment="test",
        node_id="ditto-screener-test",
        provider="test",
        provider_resource_id="resource-test",
        screener_hotkey=make_config().screener_hotkey,
        mnemonic=(
            "bottom drive obey lake curtain smoke basket hold race lonely fit walk"
        ),
        api_token=old_token,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    store_node_credential(credential_file, credential)
    calls: list[str] = []
    refresh_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_attempts
        calls.append(request.url.path)
        if request.url.path.endswith("/nodes/refresh"):
            refresh_attempts += 1
            assert request.headers["Authorization"] == f"Bearer {old_token}"
            body = json.loads(request.content)
            assert body["refresh_id"]
            if refresh_attempts == 1:
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(
                200,
                json={
                    "node_id": credential.node_id,
                    "screener_hotkey": credential.screener_hotkey,
                    "api_token": new_token,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=6)).isoformat(),
                },
            )
        assert request.headers["Authorization"] == f"Bearer {new_token}"
        return httpx.Response(
            200,
            json={
                "items": [],
                "count": 0,
                "required_policy_version": SCREENING_POLICY_VERSION,
            },
        )

    class Keypair:
        def sign(self, _message: bytes) -> bytes:
            return b"a" * 64

    cfg = make_config(node_credential_file=str(credential_file))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = PlatformClient(cfg, http, keypair=Keypair())
    async with http:
        with pytest.raises(PlatformError, match="credential refresh failed"):
            await client.get_required_policy_version()
    assert calls == ["/api/v1/screener/nodes/refresh"]
    stored = load_node_credential(credential_file)
    assert stored.api_token == old_token
    assert stored.pending_refresh_id is not None


async def test_submit_heartbeat_matches_open_platform_contract(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/screener/heartbeat"
        _assert_auth(request)
        return httpx.Response(
            200,
            json={"accepted": True, "seen_at": datetime.now(UTC).isoformat()},
        )

    client, http = _make_client(make_config(), handler)
    heartbeat = ScreenerHeartbeatRequest(
        screener_hotkey=make_config().screener_hotkey,
        software_version="0.1.0",
        protocol_version=1,
        policy_version=SCREENING_POLICY_VERSION,
        state="polling",
        timestamp=1,
        signature="ab" * 64,
    )
    async with http:
        response = await client.submit_heartbeat(heartbeat)
    assert response.accepted


async def test_get_artifact_parses_url(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    attempt_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/screener/agent/{_AGENT}/artifact"
        assert request.url.params.get("attempt_id") == str(attempt_id)
        _assert_auth(request)
        return httpx.Response(
            200,
            json={
                "agent_id": str(_AGENT),
                "sha256": "de" * 32,
                "download_url": "https://storage.test/a.tar.gz",
                "expires_at": datetime.now(UTC).isoformat(),
            },
        )

    client, http = _make_client(make_config(), handler)
    async with http:
        art = await client.get_artifact(_AGENT, attempt_id=attempt_id)
    assert str(art.download_url).startswith("https://storage.test/")


async def test_targon_build_download_is_fully_hashed_before_import(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    attempt_id = uuid4()
    build_id = uuid4()
    image = b"verified docker archive"
    digest = hashlib.sha256(image).hexdigest()
    status = {
        "build_id": str(build_id),
        "attempt_id": str(attempt_id),
        "status": "succeeded",
        "provider": "targon",
        "artifact_sha256": "de" * 32,
        "image_ref": f"ditto-screen/{_AGENT}-{attempt_id}:latest",
        "output_sha256": digest,
        "output_size_bytes": len(image),
        "download_url": "https://storage.test/image.tar",
        "error_code": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.test":
            return httpx.Response(200, content=image)
        if request.method == "POST":
            assert request.url.path.endswith("/submission-image-builds")
            return httpx.Response(200, json=status)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client, http = _make_client(make_config(), handler)
    async with http:
        archive = await client.build_submission_image(
            _AGENT, attempt_id=attempt_id, timeout=1
        )
    assert archive is not None
    try:
        assert archive.build_id == build_id
        assert Path(archive.path).read_bytes() == image
        assert archive.sha256 == digest
    finally:
        os.unlink(archive.path)


async def test_targon_build_digest_mismatch_discards_and_falls_back(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    attempt_id = uuid4()
    build_id = uuid4()
    deletes: list[str] = []
    status = {
        "build_id": str(build_id),
        "attempt_id": str(attempt_id),
        "status": "succeeded",
        "provider": "targon",
        "artifact_sha256": "de" * 32,
        "image_ref": f"ditto-screen/{_AGENT}-{attempt_id}:latest",
        "output_sha256": "ab" * 32,
        "output_size_bytes": 6,
        "download_url": "https://storage.test/image.tar",
        "error_code": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.test":
            return httpx.Response(200, content=b"tamper")
        if request.method == "POST":
            return httpx.Response(200, json=status)
        if request.method == "DELETE":
            deletes.append(request.url.path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client, http = _make_client(make_config(), handler)
    async with http:
        archive = await client.build_submission_image(
            _AGENT, attempt_id=attempt_id, timeout=1
        )
    assert archive is None
    assert deletes == [
        f"/api/v1/screener/agent/{_AGENT}/submission-image-builds/{build_id}"
    ]


async def test_remote_kaniko_failure_is_a_deterministic_build_rejection(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    attempt_id = uuid4()
    build_id = uuid4()
    status = {
        "build_id": str(build_id),
        "attempt_id": str(attempt_id),
        "status": "fallback_required",
        "provider": "hetzner",
        "artifact_sha256": "de" * 32,
        "image_ref": f"ditto-screen/{_AGENT}-{attempt_id}:latest",
        "error_code": "FLEET_SUBMISSION_KANIKO_FAILED",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json=status)

    client, http = _make_client(make_config(), handler)
    async with http:
        with pytest.raises(
            RemoteSubmissionBuildRejected,
            match="FLEET_SUBMISSION_KANIKO_FAILED",
        ):
            await client.build_submission_image(
                _AGENT, attempt_id=attempt_id, timeout=1
            )


@pytest.mark.parametrize(
    ("observation", "accepted"),
    [
        (
            {
                "ok": True,
                "risk_level": "low",
                "categories": [],
                "clearance_certified": True,
            },
            True,
        ),
        (
            {
                "ok": True,
                "risk_level": "medium",
                "categories": ["suspicious"],
                "clearance_certified": False,
            },
            True,
        ),
    ],
)
async def test_targon_source_review_returns_succeeded_observation(
    make_config: Callable[..., ScreenerConfig],
    observation: dict[str, object],
    accepted: bool,
) -> None:
    attempt_id = uuid4()
    review_id = uuid4()
    deleted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deleted
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "review_id": str(review_id),
                    "attempt_id": str(attempt_id),
                    "status": "succeeded",
                    "provider": "targon",
                    "artifact_sha256": "de" * 32,
                    "observation": observation,
                    "error_code": None,
                },
            )
        if request.method == "DELETE":
            deleted = True
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client, http = _make_client(make_config(), handler)
    async with http:
        result = await client.review_submission_source(
            _AGENT, attempt_id=attempt_id, timeout=1
        )
    assert (result is not None) is accepted
    assert deleted


async def test_submit_result_posts_signed_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/v1/screener/agent/{_AGENT}/result"
        _assert_auth(request)
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"agent_id": str(_AGENT), "status": "evaluating", "accepted": True},
        )

    client, http = _make_client(make_config(), handler)
    async with http:
        resp = await client.submit_result(
            _AGENT,
            signature="ab" * 64,
            passed=True,
            policy_version=SCREENING_POLICY_VERSION,
            detail="ok",
            attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
            outcome=ScreenResultOutcome.PASS,
            image_sha256="12" * 32,
            image_size_bytes=123,
            image_id="sha256:" + "34" * 32,
            image_ref=f"ditto-screen/{_AGENT}:latest",
            image_upload_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        )
    assert resp.accepted is True
    assert resp.status.value == "evaluating"
    assert captured["passed"] is True
    assert captured["signature"] == "ab" * 64
    assert captured["detail"] == "ok"
    assert captured["policy_version"] == SCREENING_POLICY_VERSION
    assert captured["attempt_id"] == "550e8400-e29b-41d4-a716-446655440001"


async def test_source_review_retries_transient_platform_failure(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt_id = uuid4()
    review_id = uuid4()
    polls = 0

    async def no_sleep(_delay: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "review_id": str(review_id),
                    "attempt_id": str(attempt_id),
                    "status": "running",
                    "provider": "hetzner",
                    "artifact_sha256": "de" * 32,
                    "observation": None,
                    "error_code": None,
                },
            )
        if request.method == "GET":
            polls += 1
            if polls == 1:
                return httpx.Response(502, text="rolling platform deploy")
            return httpx.Response(
                200,
                json={
                    "review_id": str(review_id),
                    "attempt_id": str(attempt_id),
                    "status": "succeeded",
                    "provider": "hetzner",
                    "artifact_sha256": "de" * 32,
                    "observation": {
                        "ok": True,
                        "risk_level": "low",
                        "categories": [],
                        "clearance_certified": True,
                    },
                    "error_code": None,
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    client, http = _make_client(make_config(), handler)
    async with http:
        result = await client.review_submission_source(
            _AGENT, attempt_id=attempt_id, timeout=1
        )
    assert result is not None
    assert result.clearance_certified is True
    assert polls == 2


async def test_submit_result_retries_transient_server_failure(
    make_config: Callable[..., ScreenerConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(502, text="temporary gateway failure")
        return httpx.Response(
            200,
            json={"agent_id": str(_AGENT), "status": "evaluating", "accepted": True},
        )

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    client, http = _make_client(make_config(), handler)
    async with http:
        response = await client.submit_result(
            _AGENT,
            signature="ab" * 64,
            passed=False,
            policy_version=SCREENING_POLICY_VERSION,
            attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
            outcome=ScreenResultOutcome.RETRYABLE_INFRA,
        )

    assert response.accepted is True
    assert calls == 3


async def test_submit_result_does_not_retry_conflict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(409, text="attempt already closed")

    client, http = _make_client(make_config(), handler)
    async with http:
        with pytest.raises(PlatformError, match="409"):
            await client.submit_result(
                _AGENT,
                signature="ab" * 64,
                passed=False,
                policy_version=SCREENING_POLICY_VERSION,
                attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
                outcome=ScreenResultOutcome.RETRYABLE_INFRA,
            )

    assert calls == 1


async def test_upload_screened_image_streams_exact_metadata_and_bytes(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"docker-image")
    seen: dict[str, object] = {}
    upload_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 300.0
        if request.url.path.endswith("/screened-image-upload"):
            return httpx.Response(
                200,
                json={
                    "image_upload_id": str(upload_id),
                    "storage_upload_id": "storage-upload",
                    "part_size_bytes": 5 * 1024**2,
                    "expires_at": datetime.now(UTC).isoformat(),
                },
            )
        if request.url.path.endswith("/part"):
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://storage.test/image.part",
                    "expires_at": datetime.now(UTC).isoformat(),
                    "required_headers": {
                        "Content-Type": "application/x-tar",
                        "Content-Length": str(len(b"docker-image")),
                    },
                },
            )
        if request.method == "PUT":
            seen["body"] = request.content
            seen["content_type"] = request.headers["Content-Type"]
            return httpx.Response(200, headers={"ETag": '"part-etag"'})
        if request.url.path.endswith("/complete"):
            import json

            seen["complete"] = json.loads(request.content)
            return httpx.Response(200, json={"verified": True})
        raise AssertionError(request.url)

    client, http = _make_client(make_config(), handler)
    async with http:
        result = await client.upload_screened_image(
            _AGENT,
            attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
            path=str(archive),
            sha256="12" * 32,
            size_bytes=len(b"docker-image"),
            image_id="sha256:" + "34" * 32,
            image_ref=f"ditto-screen/{_AGENT}:latest",
        )
    assert result == upload_id
    assert seen["body"] == b"docker-image"
    assert seen["content_type"] == "application/x-tar"
    assert seen["complete"]["parts"] == [  # type: ignore[index]
        {"part_number": 1, "etag": '"part-etag"'}
    ]


async def test_non_200_raises_platform_error(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="agent past screening")

    client, http = _make_client(make_config(), handler)
    async with http:
        with pytest.raises(PlatformError, match="409"):
            await client.submit_result(
                _AGENT,
                signature="ab" * 64,
                passed=False,
                policy_version=SCREENING_POLICY_VERSION,
                attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
                outcome=ScreenResultOutcome.DETERMINISTIC_REJECT,
            )


async def test_multipart_part_failure_is_single_shot_and_aborted(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"retry-me")
    upload_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    put_calls = 0
    aborted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal aborted, put_calls
        if request.url.path.endswith("/screened-image-upload"):
            return httpx.Response(
                200,
                json={
                    "image_upload_id": str(upload_id),
                    "storage_upload_id": "storage-upload",
                    "part_size_bytes": 5 * 1024**2,
                    "expires_at": datetime.now(UTC).isoformat(),
                },
            )
        if request.url.path.endswith("/part"):
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://storage.test/image.part",
                    "expires_at": datetime.now(UTC).isoformat(),
                    "required_headers": {},
                },
            )
        if request.method == "PUT":
            put_calls += 1
            return httpx.Response(503, text="temporary")
        if request.url.path.endswith("/abort"):
            aborted = True
            return httpx.Response(200, json={"aborted": True})
        if request.url.path.endswith("/complete"):
            return httpx.Response(200, json={"verified": True})
        raise AssertionError(request.url)

    client, http = _make_client(make_config(), handler)
    async with http:
        with pytest.raises(PlatformError, match="503"):
            await client.upload_screened_image(
                _AGENT,
                attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
                path=str(archive),
                sha256="12" * 32,
                size_bytes=archive.stat().st_size,
                image_id="sha256:" + "34" * 32,
                image_ref=f"ditto-screen/{_AGENT}:latest",
            )
    assert put_calls == 1
    assert aborted


async def test_multipart_failure_aborts_upload(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"cannot-upload")
    upload_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    aborted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal aborted
        if request.url.path.endswith("/screened-image-upload"):
            return httpx.Response(
                200,
                json={
                    "image_upload_id": str(upload_id),
                    "storage_upload_id": "storage-upload",
                    "part_size_bytes": 5 * 1024**2,
                    "expires_at": datetime.now(UTC).isoformat(),
                },
            )
        if request.url.path.endswith("/part"):
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://storage.test/image.part",
                    "expires_at": datetime.now(UTC).isoformat(),
                    "required_headers": {},
                },
            )
        if request.method == "PUT":
            return httpx.Response(403, text="expired")
        if request.url.path.endswith("/abort"):
            aborted = True
            return httpx.Response(200, json={"aborted": True})
        raise AssertionError(request.url)

    client, http = _make_client(make_config(), handler)
    async with http:
        with pytest.raises(PlatformError, match=r"part 1 upload rejected \(403\)"):
            await client.upload_screened_image(
                _AGENT,
                attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
                path=str(archive),
                sha256="12" * 32,
                size_bytes=archive.stat().st_size,
                image_id="sha256:" + "34" * 32,
                image_ref=f"ditto-screen/{_AGENT}:latest",
            )
    assert aborted


async def test_multipart_mint_rejection_does_not_upload_or_abort(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"not-owned")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(409, text="wrong owner")

    client, http = _make_client(make_config(), handler)
    async with http:
        with pytest.raises(PlatformError, match=r"initiate rejected \(409\)"):
            await client.upload_screened_image(
                _AGENT,
                attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
                path=str(archive),
                sha256="12" * 32,
                size_bytes=archive.stat().st_size,
                image_id="sha256:" + "34" * 32,
                image_ref=f"ditto-screen/{_AGENT}:latest",
            )
    assert calls == 1
