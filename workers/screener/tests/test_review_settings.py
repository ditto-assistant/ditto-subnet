"""Dynamic review settings validation, caching, and fail-safe behavior."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from ditto_screener.errors import PlatformError
from ditto_screener.platform import PlatformClient
from ditto_screener.review_settings import (
    CachedReviewSettings,
    ReviewSettingsCache,
    ShadowReviewObservationRequest,
    ShadowReviewUsage,
    bootstrap_review_settings,
)


def _shadow_observation(*, stages: int) -> ShadowReviewObservationRequest:
    return ShadowReviewObservationRequest(
        attempt_id=UUID("96af45fd-65da-4f59-87f8-8ddf5d57f88c"),
        artifact_sha256="ab" * 32,
        settings_revision=1,
        settings_scope="*",
        settings_checksum="cd" * 32,
        disposition="safe",
        risk_level="low",
        response_models=tuple(f"model-{index}" for index in range(stages)),
        response_providers=tuple(f"provider-{index}" for index in range(stages)),
        usage=ShadowReviewUsage(
            input_tokens=1,
            output_tokens=1,
            cached_input_tokens=0,
            reasoning_tokens=0,
            estimated_cost_usd=0,
        ),
    )


def test_shadow_observation_allows_fifty_provider_stages() -> None:
    assert len(_shadow_observation(stages=50).response_models) == 50


def test_shadow_observation_rejects_more_than_fifty_provider_stages() -> None:
    with pytest.raises(ValidationError, match="too many provider stages"):
        _shadow_observation(stages=51)


@pytest.mark.asyncio
async def test_platform_settings_are_cached_and_apply_every_budget(
    make_config, tmp_path
) -> None:
    config = make_config(
        review_settings_cache_file=str(tmp_path / "settings.json"),
        l2_review_mode="off",
    )
    baseline = bootstrap_review_settings(config)
    body = baseline.model_copy(
        update={
            "revision": 42,
            "scope": "ditto-screener-prod",
            "settings": baseline.settings.model_copy(
                update={
                    "mode": "shadow",
                    "max_steps": 9,
                    "max_cost_usd": 0.75,
                    "critic_reasoning_effort": "low",
                }
            ),
        }
    )
    # model_copy does not rerun checksum validation; bind the changed payload.
    serialized = body.settings.model_dump(mode="json")
    checksum = hashlib.sha256(
        json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    body = body.model_copy(update={"checksum": checksum})
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=body.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(config, http)
        first = await client.get_review_settings("ditto-screener-prod")
        second = await client.get_review_settings("ditto-screener-prod")

    assert calls == 1
    assert first == second
    runtime = first.apply_to(config)
    assert runtime.l2_review_mode == "shadow"
    assert runtime.l2_max_steps == 9
    assert runtime.l2_max_cost_usd == 0.75
    assert runtime.l2_critic_reasoning_effort == "low"
    assert ReviewSettingsCache(config.review_settings_cache_file).load() is not None


@pytest.mark.asyncio
async def test_expired_enforce_cache_refuses_new_claims(make_config, tmp_path) -> None:
    config = make_config(
        review_settings_cache_file=str(tmp_path / "settings.json"),
        review_settings_max_stale_seconds=60,
    )
    baseline = bootstrap_review_settings(replace(config, l2_review_mode="enforce"))
    cache = ReviewSettingsCache(config.review_settings_cache_file)
    cache.store(baseline)
    stored = cache.load()
    assert stored is not None
    (tmp_path / "settings.json").write_text(
        CachedReviewSettings(
            cached_at=int(time.time()) - 61,
            effective=stored.effective,
        ).model_dump_json(),
        encoding="utf-8",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("platform unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(config, http)
        with pytest.raises(PlatformError, match="expired"):
            await client.get_review_settings("ditto-screener-prod")


@pytest.mark.asyncio
async def test_invalid_remote_checksum_falls_back_to_local_off(
    make_config, tmp_path
) -> None:
    config = make_config(
        review_settings_cache_file=str(tmp_path / "settings.json"),
        l2_review_mode="off",
    )
    body = bootstrap_review_settings(config).model_dump(mode="json")
    body["checksum"] = "0" * 64

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(config, http)
        result = await client.get_review_settings("ditto-screener-prod")
    assert result.scope == "bootstrap"
    assert result.settings.mode == "off"
