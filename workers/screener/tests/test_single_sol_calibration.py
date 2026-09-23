from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest

from ditto_screener.source_review import OpenRouterSourceReviewAgent
from ditto_screening_protocol.models import source_review_invariants_for_policy


def _calibration_module() -> Any:
    script = Path(__file__).parents[1] / "scripts" / "run_single_sol_calibration.py"
    spec = importlib.util.spec_from_file_location("run_single_sol_calibration", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reviewer() -> Any:
    return _calibration_module().MeteredSingleSolReviewer(
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


def test_invariant_assessment_exports_only_bounded_decisions() -> None:
    finding = {
        "artifact_sha256": "a" * 64,
        "prompt_revision": "source-review-v13",
        "risk_level": "low",
        "confidence": 0.8,
        "categories": ["none"],
        "evidence": [{"path": "src/main.py", "line": 42, "category": "none"}],
        "summary": "No causal policy breach established.",
        "invariant_assessment": {
            "schema_version": 2,
            "decisions": [
                {
                    "invariant": invariant.value,
                    "disposition": "breach" if index == 0 else "inconclusive",
                    "summary": "Not established in this fixture.",
                    "evidence_indices": [0] if index == 0 else [],
                }
                for index, invariant in enumerate(
                    source_review_invariants_for_policy(13)
                )
            ],
        },
        "unexpected_secret": "must not be exported",
    }

    result = _calibration_module()._sanitized_invariant_assessment(finding)

    assert result is not None
    assert result["schema_version"] == 2
    assert len(result["decisions"]) == 8
    assert result["decisions"][0]["disposition"] == "breach"
    assert result["decisions"][0]["evidence"] == [
        {"path": "src/main.py", "line": 42, "category": "none"}
    ]
    assert "summary" not in result["decisions"][0]
    assert len(result["decisions"][0]["summary_sha256"]) == 64
    assert all(
        decision["disposition"] == "inconclusive"
        for decision in result["decisions"][1:]
    )
    assert "must not be exported" not in str(result)
    assert _calibration_module()._sanitized_invariant_assessment(None) is None
