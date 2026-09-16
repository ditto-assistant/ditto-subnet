"""Router fallback receives the actual bounded buffered request lifetime."""

import asyncio
import json

import httpx
import pytest

from ditto_screener.fanout_review import (
    ExperimentalReviewer,
    FanoutBudget,
    FanoutBudgetExhausted,
)
from ditto_screener.source_review import OpenRouterSourceReviewAgent


def _reviewer(provider="ditto", **kwargs):
    return ExperimentalReviewer(
        focus="deadline regression",
        max_steps=12,
        api_key_file=None,
        model="z-ai/glm-5.3-flash",
        base_url="https://router.example/v1",
        inference_provider=provider,
        timeout_seconds=120,
        max_completion_request_seconds=120,
        transport_retry_delays=(),
        **kwargs,
    )


@pytest.mark.parametrize(
    "remaining,expected", [(120, "120000"), (2.3459, "2345"), (0.02, "20")]
)
async def test_ditto_hint_matches_effective_remaining_deadline(remaining, expected):
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "glm-5.3-flash",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001},
            },
        )

    reviewer = _reviewer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await reviewer._post_completion(
            client,
            "test-key",
            [{"role": "system", "content": "Review inert source."}],
            timeout=remaining,
            reasoning_effort="low",
        )
    assert seen[0].headers["X-Ditto-Request-Timeout-Ms"] == expected
    body = json.loads(seen[0].content)
    assert not body.get("stream")
    assert body["max_completion_tokens"] == reviewer._max_completion_tokens


def test_hint_never_expands_shadow_cap_or_leaks_to_other_provider():
    assert (
        _reviewer()._completion_request_headers("key", 900)[
            "X-Ditto-Request-Timeout-Ms"
        ]
        == "120000"
    )
    assert "X-Ditto-Request-Timeout-Ms" not in _reviewer(
        "openrouter"
    )._completion_request_headers("key", 120)
    authoritative = OpenRouterSourceReviewAgent(
        api_key_file=None,
        inference_provider="ditto",
        max_steps=12,
        model="model",
        base_url="https://router.example/v1",
        timeout_seconds=120,
    )
    assert (
        "X-Ditto-Request-Timeout-Ms"
        not in authoritative._completion_request_headers("key", 120)
    )


async def test_cancelled_buffered_request_remains_unmetered_and_cannot_retry():
    budget = FanoutBudget(
        max_requests=3, max_total_tokens=200_000, max_reported_cost_usd=3
    )
    cancelled = asyncio.Event()
    requests = 0

    async def handler(_request):
        nonlocal requests
        requests += 1
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
        raise AssertionError("must cancel before receiving any completion")

    reviewer = _reviewer(budget=budget)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await reviewer._post_completion(
                client,
                "key",
                [{"role": "system", "content": "Review."}],
                timeout=0.01,
                reasoning_effort="low",
            )
        assert cancelled.is_set()
        assert budget.snapshot()["unmetered_responses"] == 1
        with pytest.raises(FanoutBudgetExhausted, match="metering"):
            await reviewer._post_completion(
                client,
                "key",
                [{"role": "system", "content": "Review."}],
                timeout=0.01,
                reasoning_effort="low",
            )
    assert requests == 1
