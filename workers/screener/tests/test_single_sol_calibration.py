from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from ditto_screener.source_review import OpenRouterSourceReviewAgent


def _reviewer() -> Any:
    script = Path(__file__).parents[1] / "scripts" / "run_single_sol_calibration.py"
    spec = importlib.util.spec_from_file_location("run_single_sol_calibration", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MeteredSingleSolReviewer(
        api_key_file=None,
        model="openai/gpt-5.6-sol",
        base_url="https://openrouter.ai/api/v1",
        timeout_seconds=1,
        max_steps=1,
    )


@pytest.mark.asyncio
async def test_single_sol_usage_counts_reported_cost_and_unmetered_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter(
        [
            httpx.Response(
                200,
                json={
                    "model": "openai/gpt-5.6-sol",
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "cost": 0.1,
                    },
                },
            ),
            httpx.Response(200, json={"model": "openai/gpt-5.6-sol"}),
        ]
    )

    async def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
        return next(replies)

    monkeypatch.setattr(OpenRouterSourceReviewAgent, "_post_completion", fake_post)
    reviewer = _reviewer()
    async with httpx.AsyncClient() as client:
        for _ in range(2):
            await reviewer._post_completion(
                client, "placeholder", [], reasoning_effort="high"
            )

    assert reviewer.usage == {
        "input_tokens": 12,
        "cached_input_tokens": 0,
        "output_tokens": 3,
        "reasoning_tokens": 0,
        "reported_cost_usd": 0.1,
        "requests": 2,
        "responses": 2,
        "request_failures": 0,
        "unmetered_responses": 1,
    }


@pytest.mark.asyncio
async def test_single_sol_usage_counts_failed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_post(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout")

    monkeypatch.setattr(OpenRouterSourceReviewAgent, "_post_completion", fake_post)
    reviewer = _reviewer()
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ReadTimeout):
            await reviewer._post_completion(
                client, "placeholder", [], reasoning_effort="high"
            )

    assert reviewer.usage["requests"] == 1
    assert reviewer.usage["responses"] == 0
    assert reviewer.usage["request_failures"] == 1
