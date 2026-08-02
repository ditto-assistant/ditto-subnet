"""Tests for the screener platform HTTP client (mocked transport)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from ditto_screener import platform as platform_module
from ditto_screener.config import ScreenerConfig
from ditto_screener.errors import PlatformError
from ditto_screener.heartbeat import ScreenerHeartbeatRequest
from ditto_screener.platform import PlatformClient
from ditto_screening_protocol import SCREENING_POLICY_VERSION, ScreenResultOutcome

_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_TOKEN = "test-screener-token-at-least-32-characters"


def test_verdict_retry_window_spans_a_normal_platform_rollout() -> None:
    assert sum(platform_module._VERDICT_RETRY_DELAYS_SECONDS) == 61.5
    assert len(platform_module._VERDICT_RETRY_DELAYS_SECONDS) + 1 == 8


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
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/screener/claim"
        assert request.url.params["policy_version"] == str(SCREENING_POLICY_VERSION)
        _assert_auth(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "agent_id": str(_AGENT),
                        "miner_hotkey": _MINER,
                        "name": "alpha",
                        "sha256": "de" * 32,
                        "status": "screening",
                        "created_at": "2026-07-06T12:00:00Z",
                        "attempt_id": "550e8400-e29b-41d4-a716-446655440001",
                        "lease_deadline": "2026-07-06T12:30:00Z",
                    }
                ],
                "count": 1,
                "required_policy_version": SCREENING_POLICY_VERSION,
            },
        )

    client, http = _make_client(make_config(), handler)
    async with http:
        resp = await client.claim_next(policy_version=SCREENING_POLICY_VERSION)
    assert resp.count == 1
    assert resp.items[0].agent_id == _AGENT
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


async def test_submit_result_retries_transient_server_failure(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
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

    monkeypatch.setattr(platform_module, "_VERDICT_RETRY_DELAYS_SECONDS", (0, 0))
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

    assert calls == 3
    assert response.accepted is True


async def test_submit_result_does_not_retry_conflict(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(409, text="attempt already closed")

    monkeypatch.setattr(platform_module, "_VERDICT_RETRY_DELAYS_SECONDS", (0, 0))
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


async def test_multipart_part_retries_transient_failure(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"retry-me")
    upload_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    put_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal put_calls
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
            if put_calls < 3:
                return httpx.Response(503, text="temporary")
            return httpx.Response(200, headers={"ETag": '"etag"'})
        if request.url.path.endswith("/complete"):
            return httpx.Response(200, json={"verified": True})
        raise AssertionError(request.url)

    client, http = _make_client(make_config(), handler)
    async with http:
        assert (
            await client.upload_screened_image(
                _AGENT,
                attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
                path=str(archive),
                sha256="12" * 32,
                size_bytes=archive.stat().st_size,
                image_id="sha256:" + "34" * 32,
                image_ref=f"ditto-screen/{_AGENT}:latest",
            )
            == upload_id
        )
    assert put_calls == 3


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
