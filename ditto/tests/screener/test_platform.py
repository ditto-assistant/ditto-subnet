"""Tests for the screener platform HTTP client (mocked transport)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.screener.config import ScreenerConfig
from ditto.screener.errors import PlatformError
from ditto.screener.platform import PlatformClient

_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_TOKEN = "test-screener-token-at-least-32-characters"


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
        resp = await client.claim_next()
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


async def test_get_artifact_parses_url(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/screener/agent/{_AGENT}/artifact"
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
        art = await client.get_artifact(_AGENT)
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
            detail="ok",
            attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
        )
    assert resp.accepted is True
    assert resp.status.value == "evaluating"
    assert captured["passed"] is True
    assert captured["signature"] == "ab" * 64
    assert captured["detail"] == "ok"
    assert captured["policy_version"] == SCREENING_POLICY_VERSION
    assert captured["attempt_id"] == "550e8400-e29b-41d4-a716-446655440001"


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
                passed=True,
                attempt_id=UUID("550e8400-e29b-41d4-a716-446655440001"),
            )
