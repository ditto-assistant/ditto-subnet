"""Real-Postgres proof that inference reservations serialize per grant only.

``begin_inference_request`` used to open with
``pg_advisory_xact_lock(hashtextextended('inference', 0))`` -- a constant key,
so one lock for every reservation on the platform, held for the whole
transaction. It was never what protected the money-critical invariants (the
grant row lock is), and it put a hard ceiling on horizontal scaling: a second
process serving the same database would still have queued behind it, which
would have neutered the whole point of running the inference plane separately.

These tests pin both halves of the replacement contract against a live
Postgres, because SQLite cannot exhibit the behavior either way:

* reservations against DIFFERENT grants make progress simultaneously, and
* reservations against the SAME grant still serialize and still cannot
  collectively over-reserve past that grant's token budget.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.config import InferenceProxyConfig
from ditto.api_server.inference_routing import benchmark_model
from ditto.db import create_db_engine
from ditto.db.models import (
    Agent,
    InferenceGrant,
    InferenceProviderRoute,
    InferenceRoutingPolicy,
    ValidatorTicket,
)
from ditto.db.queries.inference import (
    activate_inference_grant,
    begin_inference_request,
    ensure_inference_grant,
    revoke_ticket_inference,
)

pytestmark = pytest.mark.integration

_BARRIER_TIMEOUT = 5.0
# The era the leases below are for. It used to be 5, chosen for no reason; the
# ``validator_tickets`` floor trigger refuses to create a lease under
# MIN_SCOREABLE_BENCH_VERSION now, so it is the live era -- which also means the
# grant is minted against a real dynamic route (see ``_seed_route``).
_BENCH_VERSION = 7
_CHAT_MODEL = benchmark_model(_BENCH_VERSION)


async def _seed_route(session) -> None:
    """The calibrated route a v7 grant binds at mint time.

    ``ensure_inference_grant`` refuses to mint for v7 or later without one:
    there is no static provider any more. It is a precondition of every grant
    here rather than anything these tests assert, and it is fleet-wide, so
    seeding is idempotent -- each test mints two grants.
    """
    now = datetime.now(UTC)
    if await session.get(InferenceRoutingPolicy, _CHAT_MODEL) is not None:
        return
    session.add(
        InferenceRoutingPolicy(
            model=_CHAT_MODEL,
            enabled=True,
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
            model=_CHAT_MODEL,
            provider="calibrated-provider",
            profile_revision="openrouter-route-fixture-v1",
            status="healthy",
            calibration_status="eligible",
            prompt_price_per_token=0.00000003,
            completion_price_per_token=0.00000013,
            ewma_tokens_per_second=150,
            ewma_latency_ms=900,
            ewma_error_rate=0,
            ewma_timeout_rate=0,
            calibration_tool_accuracy=0.65,
            calibration_composite=0.20,
            calibration_sample_count=60,
            calibration_manifest_sha256="ab" * 32,
            sample_count=20,
            discovered_at=now,
        )
    )
    await session.flush()


def _config() -> InferenceProxyConfig:
    """Budgets deliberately generous so only the rail under test can bind."""
    return InferenceProxyConfig(
        enabled=True,
        required=False,
        public_base_url="https://platform.example",
        openrouter_api_key="test-key",
        upstream_url="https://openrouter.ai/api/v1/chat/completions",
        allowed_models=(_CHAT_MODEL,),
        provider="nebius",
        routing_mode="adaptive",
        request_budget=1000,
        token_budget=1_000_000,
        embedding_upstream_url="https://openrouter.ai/api/v1/embeddings",
        embedding_model="perplexity/pplx-embed-v1-0.6b",
        embedding_profile="dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1",
        embedding_provider="Perplexity",
        embedding_dimensions=768,
        embedding_request_budget=100_000,
        embedding_token_budget=1_000_000_000,
        embedding_per_ticket_concurrency=64,
        embedding_per_validator_concurrency=256,
        embedding_global_concurrency=1024,
        embedding_per_ticket_requests_per_minute=10_000,
        embedding_per_validator_requests_per_minute=40_000,
        embedding_global_requests_per_minute=100_000,
        embedding_request_body_bytes=1 << 20,
        embedding_response_body_bytes=16 << 20,
        per_ticket_concurrency=64,
        per_validator_concurrency=256,
        global_concurrency=1024,
        per_ticket_requests_per_minute=10_000,
        per_validator_requests_per_minute=40_000,
        global_requests_per_minute=100_000,
        request_body_bytes=1 << 20,
        response_body_bytes=1 << 20,
        timeout_seconds=10,
        max_output_tokens=1024,
    )


async def _seed_grant(maker, *, validator_hotkey: str, config) -> tuple[UUID, str]:
    """Create one agent + issued ticket + activated grant. Returns (id, bearer)."""
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey=f"miner-{validator_hotkey}",
            name=f"inference-concurrency-{validator_hotkey}",
            sha256=uuid4().hex + uuid4().hex,
            status=AgentStatus.EVALUATING,
            created_at=now,
        )
        ticket = ValidatorTicket(
            agent_id=agent.agent_id,
            validator_hotkey=validator_hotkey,
            slot_id="slot-0",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=20),
            bench_version=_BENCH_VERSION,
            attempt_count=1,
        )
        session.add_all([agent, ticket])
        await _seed_route(session)
        await session.flush()
        grant = await ensure_inference_grant(session, ticket=ticket, config=config)
        assert grant is not None
        activated = await activate_inference_grant(
            session,
            grant_id=grant.grant_id,
            validator_hotkey=validator_hotkey,
            broker_public_key=f"broker-{validator_hotkey}",
            now=now,
            config=config,
        )
        assert activated is not None
        return activated[0].grant_id, activated[1]


async def _clean(maker) -> None:
    async with maker() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE agents CASCADE"))


async def test_reservations_on_different_grants_are_not_serialized() -> None:
    """Two grants reserve simultaneously; neither waits for the other to commit.

    The barrier is the actual assertion. Each task reserves and then waits for
    the other to have reserved too, all before either transaction commits. Under
    the old global advisory lock the second task could not even enter
    ``begin_inference_request`` until the first committed, so the barrier could
    never be satisfied and this test would time out rather than merely being
    slow. That makes it a direct proof of parallelism, not a timing heuristic.
    """
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    config = _config()
    try:
        await _clean(maker)
        first = await _seed_grant(maker, validator_hotkey="validator-a", config=config)
        second = await _seed_grant(maker, validator_hotkey="validator-b", config=config)
        barrier = asyncio.Barrier(2)

        async def reserve(grant_id: UUID, bearer: str) -> bool:
            async with maker() as session, session.begin():
                reserved = await begin_inference_request(
                    session,
                    grant_id=grant_id,
                    nonce=uuid4(),
                    bearer=bearer,
                    model=_CHAT_MODEL,
                    token_reservation=16,
                    now=datetime.now(UTC),
                    config=config,
                )
                # Still inside the transaction, holding this grant's row lock.
                async with asyncio.timeout(_BARRIER_TIMEOUT):
                    await barrier.wait()
                # `is not None` would now also be true for an InferenceDecline,
                # which is a refusal. Only a tuple is an actual reservation.
                return isinstance(reserved, tuple)

        outcomes = await asyncio.gather(reserve(*first), reserve(*second))
        assert outcomes == [True, True]
    finally:
        await _clean(maker)
        await engine.dispose()


async def test_reservations_on_one_grant_serialize_and_respect_the_budget() -> None:
    """Concurrent reservations on a single grant cannot over-reserve it.

    The grant row lock is the only thing standing between eight simultaneous
    requests and a miner spending more inference than its ticket bought, so
    this is the invariant the advisory lock removal must not have weakened.
    """
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # Room for exactly three reservations of 100 tokens each.
    config = replace(_config(), token_budget=300)
    try:
        await _clean(maker)
        grant_id, bearer = await _seed_grant(
            maker, validator_hotkey="validator-solo", config=config
        )

        async def reserve() -> bool:
            async with maker() as session, session.begin():
                reserved = await begin_inference_request(
                    session,
                    grant_id=grant_id,
                    nonce=uuid4(),
                    bearer=bearer,
                    model=_CHAT_MODEL,
                    token_reservation=100,
                    now=datetime.now(UTC),
                    config=config,
                )
                # `is not None` would now also be true for an InferenceDecline,
                # which is a refusal. Only a tuple is an actual reservation.
                return isinstance(reserved, tuple)

        outcomes = await asyncio.gather(*(reserve() for _ in range(8)))
        assert sum(outcomes) == 3, "over- or under-reserved against a 300 token budget"

        async with maker() as session:
            grant = await session.get(InferenceGrant, grant_id)
            assert grant is not None
            assert grant.request_count == 3
            assert grant.active_requests == 3
    finally:
        await _clean(maker)
        await engine.dispose()


async def test_revocation_and_reservation_on_one_grant_are_mutually_exclusive() -> None:
    """A reservation racing a revocation must not charge a dead grant.

    Both paths take ``FOR UPDATE`` on the same grant row, so Postgres orders
    them. Whichever runs second sees the other's committed state: either the
    reservation lands on a still-live grant, or the revocation has already
    marked it revoked and the reservation is refused.
    """
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    config = _config()
    try:
        await _clean(maker)
        grant_id, bearer = await _seed_grant(
            maker, validator_hotkey="validator-race", config=config
        )

        async def reserve() -> bool:
            async with maker() as session, session.begin():
                reserved = await begin_inference_request(
                    session,
                    grant_id=grant_id,
                    nonce=uuid4(),
                    bearer=bearer,
                    model=_CHAT_MODEL,
                    token_reservation=16,
                    now=datetime.now(UTC),
                    config=config,
                )
                # `is not None` would now also be true for an InferenceDecline,
                # which is a refusal. Only a tuple is an actual reservation.
                return isinstance(reserved, tuple)

        async def revoke() -> None:
            async with maker() as session, session.begin():
                ticket = await session.scalar(
                    select(ValidatorTicket).where(
                        ValidatorTicket.validator_hotkey == "validator-race"
                    )
                )
                assert ticket is not None
                await revoke_ticket_inference(
                    session, ticket=ticket, now=datetime.now(UTC)
                )

        reserved, _ = await asyncio.gather(reserve(), revoke())

        async with maker() as session:
            grant = await session.get(InferenceGrant, grant_id)
            assert grant is not None
            # Revocation always wins the end state; the only question is whether
            # the reservation got in first. Either way the grant is revoked and
            # its in-flight counter is not left dangling above the request count.
            assert grant.status == "revoked"
            assert grant.active_requests >= 0
            assert grant.request_count == (1 if reserved else 0)
    finally:
        await _clean(maker)
        await engine.dispose()
