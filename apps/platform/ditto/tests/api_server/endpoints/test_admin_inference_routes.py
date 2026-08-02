"""HTTP contracts for adaptive inference policy and route admission."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.inference_routing import aggregate_profile_revision
from ditto.db.models import (
    Agent,
    InferenceGrant,
    InferenceProviderRoute,
    InferenceRequest,
    InferenceRoutingPolicy,
    ValidatorTicket,
)

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_MODEL = "openai/gpt-oss-20b"
_PROFILE = "openrouter-route-test-v1"


async def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    routing_mode: str = "adaptive",
) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token=_TOKEN,
        inference_proxy=replace(
            app.state.config.inference_proxy,
            routing_mode=routing_mode,
            reviewed_calibration_manifest_sha256="a" * 64,
        ),
    )

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            InferenceRoutingPolicy(
                model=_MODEL,
                enabled=False,
                speed_weight=0.65,
                cost_weight=0.25,
                exploration_weight=0.10,
                exploration_ticket_budget=3,
                min_tool_accuracy=0.55,
                min_composite=0.15,
                min_calibration_samples=20,
                max_error_rate=0.25,
                max_timeout_rate=0.15,
                cooldown_seconds=30,
                ewma_alpha=0.20,
                updated_at=now,
            )
        )
        session.add(
            InferenceProviderRoute(
                model=_MODEL,
                provider="Groq",
                profile_revision=_PROFILE,
                status="healthy",
                calibration_status="shadow",
                quantization="fp8",
                prompt_price_per_token=0.000000075,
                completion_price_per_token=0.0000003,
                ewma_error_rate=0,
                ewma_timeout_rate=0,
                sample_count=0,
                selected_ticket_count=0,
                exploration_ticket_count=0,
                discovered_at=now,
                updated_at=now,
            )
        )


async def _seed_grant(session: AsyncSession) -> UUID:
    """Create the agent/ticket/grant chain one inference request hangs off.

    ``inference_requests.grant_id`` is a real foreign key into
    ``inference_grants``, which in turn keys into ``validator_tickets``. The
    parent rows used to be skipped because SQLite leaves foreign keys off,
    so this file's telemetry fixture described a row shape production cannot
    produce. Nothing about what the test asserts depends on the parents --
    only that the child is insertable at all.
    """
    now = datetime.now(UTC)
    deadline = now + timedelta(minutes=20)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5MinerHotkeyInferenceTelemetry",
        name="inference-telemetry",
        version=1,
        sha256="ab" * 32,
        status=AgentStatus.EVALUATING,
        created_at=now,
    )
    ticket = ValidatorTicket(
        agent_id=agent.agent_id,
        validator_hotkey="5ValidatorHotkeyInferenceTelemetry",
        slot_id="slot-0",
        status=TicketStatus.ISSUED,
        issued_at=now,
        deadline=deadline,
        bench_version=7,
        attempt_count=1,
    )
    grant = InferenceGrant(
        grant_id=uuid4(),
        agent_id=agent.agent_id,
        bench_version=ticket.bench_version,
        validator_hotkey=ticket.validator_hotkey,
        slot_id=ticket.slot_id,
        ticket_deadline=deadline,
        status="active",
        generation=1,
        allowed_models=[_MODEL],
        request_budget=10,
        token_budget=100_000,
        embedding_model="perplexity/pplx-embed-v1-0.6b",
        embedding_profile="dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1",
        embedding_provider="Perplexity",
        embedding_dimensions=768,
        embedding_request_budget=1_000,
        embedding_token_budget=1_000_000,
        expires_at=deadline,
    )
    session.add_all([agent, ticket])
    await session.flush()
    session.add(grant)
    await session.flush()
    return grant.grant_id


def _policy_payload() -> dict[str, object]:
    return {
        "enabled": True,
        "expected_revision": 0,
        "speed_weight": 0.65,
        "cost_weight": 0.25,
        "exploration_weight": 0.10,
        "exploration_ticket_budget": 3,
        "min_tool_accuracy": 0.55,
        "min_composite": 0.15,
        "min_calibration_samples": 20,
        "max_error_rate": 0.25,
        "max_timeout_rate": 0.15,
        "cooldown_seconds": 30,
        "ewma_alpha": 0.20,
        "confirmation": f"UPDATE INFERENCE POLICY {_MODEL}",
    }


async def test_lists_and_updates_complete_model_policy(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _install(app, session_maker)
    listing = await client.get("/api/v1/admin/inference-routes", headers=_HEADERS)
    assert listing.status_code == 200
    assert listing.headers["Cache-Control"] == "no-store"
    assert listing.json()["policies"][0]["enabled"] is False
    assert listing.json()["routes"][0]["profile_revision"] == _PROFILE

    response = await client.put(
        f"/api/v1/admin/inference-routes/policy/{_MODEL}",
        headers=_HEADERS,
        json=_policy_payload(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"model": _MODEL, "enabled": True, "revision": 1}
    stale = await client.put(
        f"/api/v1/admin/inference-routes/policy/{_MODEL}",
        headers=_HEADERS,
        json=_policy_payload(),
    )
    assert stale.status_code == 409
    audited = await client.get("/api/v1/admin/inference-routes", headers=_HEADERS)
    assert audited.json()["audits"][0]["action"] == "policy_updated"
    assert audited.json()["audits"][0]["actor"] == "operator"


async def test_route_admission_requires_exact_confirmation_and_quality_floor(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _install(app, session_maker)
    payload = {
        "model": _MODEL,
        "provider": "Groq",
        "expected_revision": 0,
        "action": "eligible",
        "manifest_sha256": "a" * 64,
        "tool_accuracy": 0.65,
        "composite": 0.20,
        "sample_count": 60,
        "confirmation": "wrong",
    }
    rejected = await client.post(
        f"/api/v1/admin/inference-routes/{_PROFILE}/calibration",
        headers=_HEADERS,
        json=payload,
    )
    assert rejected.status_code == 409

    payload["confirmation"] = f"ELIGIBLE INFERENCE ROUTE {_PROFILE}"
    payload["manifest_sha256"] = "b" * 64
    unreviewed = await client.post(
        f"/api/v1/admin/inference-routes/{_PROFILE}/calibration",
        headers=_HEADERS,
        json=payload,
    )
    assert unreviewed.status_code == 409
    assert "deployed reviewed artifact" in unreviewed.json()["message"]

    payload["manifest_sha256"] = "a" * 64
    accepted = await client.post(
        f"/api/v1/admin/inference-routes/{_PROFILE}/calibration",
        headers=_HEADERS,
        json=payload,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["calibration_status"] == "eligible"
    assert accepted.json()["calibration_revision"] == 1
    audited = await client.get("/api/v1/admin/inference-routes", headers=_HEADERS)
    assert audited.json()["audits"][0]["action"] == "route_eligible"


async def test_aggregate_mode_blocks_adaptive_controls_but_allows_logical_route(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _install(app, session_maker, routing_mode="aggregate_throughput")
    profile = aggregate_profile_revision(_MODEL)
    async with session_maker() as session, session.begin():
        session.add(
            InferenceProviderRoute(
                model=_MODEL,
                provider="openrouter",
                profile_revision=profile,
                status="healthy",
                calibration_status="shadow",
                ewma_error_rate=0,
                ewma_timeout_rate=0,
                sample_count=0,
                selected_ticket_count=0,
                exploration_ticket_count=0,
                discovered_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        grant_id = await _seed_grant(session)
        session.add(
            InferenceRequest(
                grant_id=grant_id,
                nonce=uuid4(),
                generation=1,
                status="completed",
                model=_MODEL,
                reserved_tokens=100,
                prompt_tokens=80,
                completion_tokens=20,
                cost_microusd=123,
                upstream_provider="WandB",
                upstream_attempts=1,
                timed_out=False,
                latency_ms=250,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )
    listing = await client.get("/api/v1/admin/inference-routes", headers=_HEADERS)
    assert listing.json()["routing_mode"] == "aggregate_throughput"
    assert listing.json()["aggregate_route"] == {
        "model": _MODEL,
        "provider": "openrouter",
        "profile_revision": profile,
        "provider_sort": "throughput",
        "provider_order": [],
        "reliability_provider_order": ["DeepInfra", "Groq"],
        "ignored_providers": ["CoreWeave"],
        "allow_fallbacks": True,
    }
    assert listing.json()["provider_telemetry"] == [
        {
            "provider": "WandB",
            "request_count": 1,
            "completed_count": 1,
            "failed_count": 0,
            "inflight_count": 0,
            "timeout_count": 0,
            "upstream_attempt_count": 1,
            "openrouter_attempt_count": 0,
            "recovered_after_fallback_count": 0,
            "terminal_failure_count": 0,
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "cost_microusd": 123,
            "average_latency_ms": 250.0,
            "observed_output_tps": 80.0,
        }
    ]
    blocked = await client.put(
        f"/api/v1/admin/inference-routes/policy/{_MODEL}",
        headers=_HEADERS,
        json=_policy_payload(),
    )
    assert blocked.status_code == 409
    provider_payload = {
        "model": _MODEL,
        "provider": "Groq",
        "expected_revision": 0,
        "action": "eligible",
        "manifest_sha256": "a" * 64,
        "tool_accuracy": 0.65,
        "composite": 0.20,
        "sample_count": 60,
        "confirmation": f"ELIGIBLE INFERENCE ROUTE {_PROFILE}",
    }
    blocked_provider = await client.post(
        f"/api/v1/admin/inference-routes/{_PROFILE}/calibration",
        headers=_HEADERS,
        json=provider_payload,
    )
    assert blocked_provider.status_code == 409
    provider_payload.update(
        {
            "provider": "openrouter",
            "confirmation": f"ELIGIBLE INFERENCE ROUTE {profile}",
        }
    )
    provider_payload["sample_count"] = 20
    incomplete = await client.post(
        f"/api/v1/admin/inference-routes/{profile}/calibration",
        headers=_HEADERS,
        json=provider_payload,
    )
    assert incomplete.status_code == 409
    provider_payload["sample_count"] = 60
    admitted = await client.post(
        f"/api/v1/admin/inference-routes/{profile}/calibration",
        headers=_HEADERS,
        json=provider_payload,
    )
    assert admitted.status_code == 200, admitted.text


async def test_provider_telemetry_aggregates_are_json_numbers_not_strings(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Every aggregate leaves this endpoint as a JSON number.

    Regression test for the production bug #446 found. ``func.sum()`` over a
    ``BigInteger`` is ``numeric`` in Postgres -- not ``bigint`` -- and
    ``func.avg()`` is ``numeric`` too, so asyncpg returns both as ``Decimal``;
    the handler's old ``-> dict[str, object]`` annotation then had Pydantic v2
    render each ``Decimal`` as a *string*. Backroom parses this array with
    ``z.number()`` (``admin.schemas.ts``), so the string form failed its parse
    outright.

    Type assertions rather than value equality, because ``250 == 250.0`` in
    Python: a plain ``==`` comparison against the expected dict cannot tell an
    int from a float, and would not notice the aggregates drifting back.
    """
    await _install(app, session_maker)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        grant_id = await _seed_grant(session)
        for (
            provider,
            latency,
            status,
            timed_out,
            attempts,
            router_attempts,
            fallback_phase,
            terminal_error,
        ) in (
            ("Groq", 200, "completed", False, 1, 1, 0, None),
            ("Groq", 300, "completed", False, 1, 2, 1, None),
            (None, 400, "failed", False, 1, 1, 1, "provider_unavailable"),
            ("WandB", None, "failed", True, 2, 0, 1, "provider_timeout"),
        ):
            session.add(
                InferenceRequest(
                    grant_id=grant_id,
                    nonce=uuid4(),
                    generation=1,
                    status=status,
                    model=_MODEL,
                    reserved_tokens=100,
                    prompt_tokens=80,
                    completion_tokens=20,
                    cost_microusd=123,
                    upstream_provider=provider,
                    upstream_attempts=attempts,
                    openrouter_attempts=router_attempts,
                    fallback_phase=fallback_phase,
                    terminal_error_code=terminal_error,
                    timed_out=timed_out,
                    latency_ms=latency,
                    started_at=now,
                    completed_at=now,
                )
            )

    listing = await client.get("/api/v1/admin/inference-routes", headers=_HEADERS)
    assert listing.status_code == 200, listing.text
    telemetry = listing.json()["provider_telemetry"]
    assert telemetry == [
        {
            "provider": "Groq",
            "request_count": 2,
            "completed_count": 2,
            "failed_count": 0,
            "inflight_count": 0,
            "timeout_count": 0,
            "upstream_attempt_count": 2,
            "openrouter_attempt_count": 3,
            "recovered_after_fallback_count": 1,
            "terminal_failure_count": 0,
            "prompt_tokens": 160,
            "completion_tokens": 40,
            "cost_microusd": 246,
            "average_latency_ms": 250.0,
            "observed_output_tps": 80.0,
        },
        {
            "provider": "Unknown upstream",
            "request_count": 1,
            "completed_count": 0,
            "failed_count": 1,
            "inflight_count": 0,
            "timeout_count": 0,
            "upstream_attempt_count": 1,
            "openrouter_attempt_count": 1,
            "recovered_after_fallback_count": 0,
            "terminal_failure_count": 1,
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "cost_microusd": 123,
            "average_latency_ms": 400.0,
            "observed_output_tps": None,
        },
        {
            "provider": "WandB",
            "request_count": 1,
            "completed_count": 0,
            "failed_count": 1,
            "inflight_count": 0,
            "timeout_count": 1,
            "upstream_attempt_count": 2,
            "openrouter_attempt_count": 0,
            "recovered_after_fallback_count": 0,
            "terminal_failure_count": 1,
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "cost_microusd": 123,
            # `latency_ms` is nullable, so avg() over an all-null group is
            # NULL. Null, not 0: "not measured" is not "instant".
            "average_latency_ms": None,
            "observed_output_tps": None,
        },
    ]
    groq, unknown, wandb = telemetry
    for field in (
        "request_count",
        "completed_count",
        "failed_count",
        "inflight_count",
        "timeout_count",
        "upstream_attempt_count",
        "openrouter_attempt_count",
        "recovered_after_fallback_count",
        "terminal_failure_count",
        "prompt_tokens",
        "completion_tokens",
        "cost_microusd",
    ):
        assert isinstance(groq[field], int), (field, groq[field])
    assert isinstance(groq["average_latency_ms"], float)
    assert isinstance(groq["observed_output_tps"], float)
    assert unknown["provider"] == "Unknown upstream"
    assert wandb["average_latency_ms"] is None
    # Nothing numeric arrives quoted, whatever the column type behind it.
    assert '"160"' not in listing.text
    assert "250.0000000000000000" not in listing.text


async def test_relay_recovery_telemetry_distinguishes_broker_exhaustion(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _install(app, session_maker)
    async with session_maker() as session, session.begin():
        await _seed_grant(session)
        ticket = await session.scalar(select(ValidatorTicket))
        assert ticket is not None
        ticket.failure_reason = "infrastructure"
        ticket.failure_detail = "model_relay_unavailable:provider_recovery_exhausted"

    listing = await client.get("/api/v1/admin/inference-routes", headers=_HEADERS)
    assert listing.status_code == 200, listing.text
    assert listing.json()["relay_recovery_telemetry"] == {
        "benchmark_relay_abort_ticket_count": 1,
        "broker_recovery_exhausted_ticket_count": 1,
    }
