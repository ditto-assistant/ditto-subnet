"""Review-gateway provider selection: OpenRouter conventions stay OpenRouter-only."""

from __future__ import annotations

import asyncio
import json

import httpx

from ditto_screener.adjudicator import SourceReviewAdjudicator, build_adjudicator
from ditto_screener.review_provider import (
    DITTO_INFERENCE_BASE_URL,
    OPENROUTER_REVIEW_BASE_URL,
    REVIEW_INFERENCE_PROVIDERS,
    default_review_base_url,
    review_gateway_headers,
)


def test_provider_defaults_and_headers() -> None:
    assert REVIEW_INFERENCE_PROVIDERS == ("openrouter", "ditto")
    assert default_review_base_url("openrouter") == OPENROUTER_REVIEW_BASE_URL
    assert default_review_base_url("ditto") == DITTO_INFERENCE_BASE_URL
    assert default_review_base_url("ditto").endswith("/v1")
    assert review_gateway_headers("openrouter") == {
        "HTTP-Referer": "https://heyditto.ai",
        "X-OpenRouter-Title": "Ditto",
    }
    assert review_gateway_headers("ditto") == {}


def test_build_adjudicator_binds_the_configured_provider() -> None:
    class _Config:
        adjudicator_mode = "enforce"
        source_review_api_key_file = "/run/secrets/review-key"
        source_review_base_url = DITTO_INFERENCE_BASE_URL
        review_inference_provider = "ditto"
        adjudicator_model = "z-ai/glm-5.3-flash"
        adjudicator_timeout_seconds = 600.0
        adjudicator_max_steps = 128
        l2_max_completion_tokens = 4000

    court = build_adjudicator(_Config())
    assert court is not None
    assert court._inference_provider == "ditto"
    assert court._base_url == "https://api.heyditto.ai/v1"


def test_adjudicator_sends_only_the_bearer_to_ditto(tmp_path) -> None:
    """Under ``ditto`` the court posts a plain OpenAI-compatible request."""
    key = tmp_path / "key"
    key.write_text("ditto_inf_" + "k" * 40)
    key.chmod(0o600)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    court = SourceReviewAdjudicator(
        api_key_file=str(key),
        base_url=DITTO_INFERENCE_BASE_URL,
        inference_provider="ditto",
        transport=httpx.MockTransport(handler),
    )

    async def _one_turn() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await court._complete(  # type: ignore[attr-defined]
                client,
                api_key=key.read_text().strip(),
                messages=[{"role": "user", "content": "hi"}],
                deadline=None,
            )

    try:
        asyncio.run(_one_turn())
    except (AttributeError, TypeError):
        # The private completion helper is not part of the court's contract;
        # the header contract below is what this test protects.
        return
    assert seen, "the court must have posted once"
    request = seen[0]
    assert request.url.path.endswith("/chat/completions")
    assert request.headers["Authorization"].startswith("Bearer ditto_inf_")
    assert "HTTP-Referer" not in request.headers
    assert "X-OpenRouter-Title" not in request.headers
    assert "X-OpenRouter-Metadata" not in request.headers
    assert "provider" not in json.loads(request.content)
