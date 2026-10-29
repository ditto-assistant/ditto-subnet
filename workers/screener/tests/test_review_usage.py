"""Per-attempt review usage telemetry: collection, L1 parsing, worker post."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from ditto_screener.config import ScreenerConfig
from ditto_screener.policy import ScreeningOutcome
from ditto_screener.review_usage import (
    collect_review_usage,
    l2_stage_for_role,
    record_chat_completion_usage,
    record_usage,
)
from ditto_screener.source_review import OpenRouterSourceReviewAgent
from ditto_screening_protocol import SCREENING_POLICY_VERSION
from tests.test_worker import _decision, _FakeGate, _FakePlatform, _item, _worker


def test_collector_sums_per_stage_and_model_and_keeps_only_complete_reports() -> None:
    with collect_review_usage() as usage:
        record_usage(
            "l2_analyst",
            model="openai/gpt-6-sol",
            input_tokens=100,
            output_tokens=10,
            cached_input_tokens=60,
            estimated_cost_usd=0.5,
            reported_cost_usd=0.4,
        )
        record_usage(
            "l2_analyst",
            model="openai/gpt-6-sol",
            input_tokens=50,
            output_tokens=5,
            estimated_cost_usd=0.25,
            reported_cost_usd=None,
        )
        record_usage(
            "l3_adjudicator",
            model="bad model id!",
            input_tokens=7,
            output_tokens=1,
            cached_input_tokens=99,
            reported_cost_usd=0.01,
        )
    entries = {(e.stage, e.model): e for e in usage.entries()}
    analyst = entries[("l2_analyst", "openai/gpt-6-sol")]
    assert analyst.requests == 2
    assert analyst.input_tokens == 150 and analyst.cached_input_tokens == 60
    assert analyst.estimated_cost_usd == pytest.approx(0.75)
    # One of two requests reported no cost: a partial sum would understate.
    assert analyst.reported_cost_usd is None
    adjudicator = entries[("l3_adjudicator", "unknown")]
    assert adjudicator.cached_input_tokens == 7  # clamped to input
    assert adjudicator.reported_cost_usd == pytest.approx(0.01)


def test_recording_outside_an_attempt_is_a_no_op() -> None:
    record_usage("l1", model="m", input_tokens=1, output_tokens=1)
    with collect_review_usage() as usage:
        pass
    assert usage.empty


def test_role_mapping() -> None:
    assert l2_stage_for_role("analyst") == "l2_analyst"
    assert l2_stage_for_role("critic") == "l2_critic"
    assert l2_stage_for_role("adjudicator") == "l3_adjudicator"
    assert l2_stage_for_role("violation_adjudicator") == "l3_adjudicator"
    assert l2_stage_for_role("violation_tiebreaker") == "l2_tiebreak"
    assert l2_stage_for_role("something-new") == "l2_total"


def test_chat_usage_parsing_tolerates_missing_and_malformed_fields() -> None:
    with collect_review_usage() as usage:
        record_chat_completion_usage(
            "l1",
            {
                "model": "openai/gpt-6-luna",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "prompt_tokens_details": {"cached_tokens": 800},
                    "completion_tokens_details": {"reasoning_tokens": 150},
                    "cost": 0.02,
                },
            },
            default_model="fallback",
        )
        record_chat_completion_usage("l1", {"usage": "nope"}, default_model="x")
        record_chat_completion_usage(
            "l1",
            {"usage": {"prompt_tokens": True, "completion_tokens": -3}},
            default_model="openai/gpt-6-luna",
        )
    [entry] = usage.entries()
    assert entry.model == "openai/gpt-6-luna"
    assert entry.requests == 2
    assert entry.input_tokens == 1000 and entry.cached_input_tokens == 800
    assert entry.output_tokens == 200 and entry.reasoning_tokens == 150
    assert entry.reported_cost_usd is None  # second request reported none


async def test_l1_turns_record_provider_usage_into_the_attempt_collector() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-6-luna",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 4000,
                    "completion_tokens": 300,
                    "prompt_tokens_details": {"cached_tokens": 3000},
                    "cost": 0.031,
                },
            },
        )

    agent = OpenRouterSourceReviewAgent(
        api_key_file=None,
        model="openai/gpt-6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=60,
        max_steps=1,
        transport=httpx.MockTransport(handler),
    )
    with collect_review_usage() as usage:
        async with httpx.AsyncClient(transport=agent._transport) as client:
            for _ in range(2):
                await agent._completion_message(
                    client,
                    "test-key",
                    [{"role": "user", "content": "inventory"}],
                    timeout=60,
                    reasoning_effort="high",
                )
            # The collector follows the attempt into spawned tasks.
            await asyncio.create_task(
                agent._completion_message(
                    client,
                    "test-key",
                    [{"role": "user", "content": "inventory"}],
                    timeout=60,
                    reasoning_effort="high",
                )
            )
    [entry] = usage.entries()
    assert entry.stage == "l1" and entry.requests == 3
    assert entry.input_tokens == 12_000 and entry.output_tokens == 900
    assert entry.reported_cost_usd == pytest.approx(0.093)


class _UsageGate(_FakeGate):
    async def screen(self, **kwargs: Any) -> Any:  # type: ignore[override]
        record_usage(
            "l1",
            model="openai/gpt-6-luna",
            input_tokens=900,
            output_tokens=90,
            reported_cost_usd=0.5,
        )
        record_usage(
            "l2_analyst",
            model="openai/gpt-6-sol",
            input_tokens=2000,
            output_tokens=100,
            estimated_cost_usd=1.5,
        )
        return await super().screen(**kwargs)


class _UsagePlatform(_FakePlatform):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__([])
        self.fail = fail
        self.review_usage: list[tuple[UUID, dict[str, Any]]] = []
        self.order: list[str] = []

    async def submit_review_usage(self, agent_id: UUID, request: Any) -> Any:
        self.order.append("usage")
        if self.fail:
            raise RuntimeError("private provider message must not be logged")
        self.review_usage.append((agent_id, request.model_dump(mode="json")))
        return object()

    async def submit_result(self, agent_id: UUID, **kwargs: Any) -> Any:
        self.order.append("result")
        return await super().submit_result(agent_id, **kwargs)


async def test_worker_posts_usage_before_the_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    item = _item(uuid4())
    platform = _UsagePlatform()
    gate = _UsageGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert platform.order[0] == "usage"
    [(agent_id, request)] = platform.review_usage
    assert agent_id == item.agent_id
    assert request["attempt_id"] == str(item.attempt_id)
    assert request["artifact_sha256"] == item.sha256
    assert [stage["stage"] for stage in request["stages"]] == ["l1", "l2_analyst"]
    assert request["elapsed_ms"] >= 0
    assert request["lineage_scope"] is None
    assert len(platform.verdicts) == 1 and platform.verdicts[0]["passed"] is True


async def test_usage_post_failure_never_drops_the_verdict(
    make_config: Callable[..., ScreenerConfig],
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _item(uuid4())
    platform = _UsagePlatform(fail=True)
    gate = _UsageGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    with caplog.at_level("WARNING"):
        await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.verdicts) == 1 and platform.verdicts[0]["passed"] is True
    assert "review usage telemetry failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private provider message" not in caplog.text


async def test_no_model_spend_posts_nothing(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    item = _item(uuid4())
    platform = _UsagePlatform()
    worker = _worker(
        make_config(), platform, _FakeGate(_decision(ScreeningOutcome.PASS))
    )

    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert platform.review_usage == []
    assert len(platform.verdicts) == 1
