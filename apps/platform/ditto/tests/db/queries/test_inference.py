from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.inference_concurrency_settings import (
    DEFAULT_CHAT_TOKEN_BUDGET,
    InferenceConcurrencySettings,
)
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.config import InferenceProxyConfig
from ditto.api_server.inference_concurrency_settings import apply_settings
from ditto.api_server.inference_routing import benchmark_model
from ditto.db.models import (
    Agent,
    AgentStatus,
    InferenceGrant,
    InferenceProviderRoute,
    InferenceRoutingPolicy,
    ValidatorTicket,
)
from ditto.db.queries.inference import (
    USAGE_ACCOUNTING_VERSION,
    InferenceDecline,
    activate_inference_grant,
    begin_inference_request,
    ensure_inference_grant,
    finish_inference_request,
    get_lease_model_usage,
    revoke_ticket_inference,
)

# The era every ticket below is leased for. Nothing in this file is about a
# benchmark version -- these are budget, concurrency and revocation rules -- and
# the value used to be 5 purely as a placeholder. The ``validator_tickets``
# floor trigger refuses to create a lease beneath
# MIN_SCOREABLE_BENCH_VERSION, so the placeholder is the live era, and the live
# era brings its model and its route requirement with it (see
# ``_seed_calibrated_route``).
_BENCH_VERSION = 7
_CHAT_MODEL = benchmark_model(_BENCH_VERSION)


async def _seed_calibrated_route(session: AsyncSession, now: datetime) -> None:
    """The dynamic route a v7 grant is bound to at mint time.

    From v7 on, ``ensure_inference_grant`` refuses to mint without a calibrated,
    eligible route for the benchmark's model -- there is no static provider any
    more. That is a precondition of every grant in this file now, not a thing
    any of these tests assert, so it is seeded once here rather than restated.

    Idempotent, because several tests mint a second grant for a second validator
    and the route is fleet-wide, not per-grant.
    """
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


def _config() -> InferenceProxyConfig:
    return InferenceProxyConfig(
        enabled=True,
        required=False,
        public_base_url="https://platform.example",
        openrouter_api_key="test-key",
        upstream_url="https://openrouter.ai/api/v1/chat/completions",
        allowed_models=(_CHAT_MODEL,),
        provider="nebius",
        routing_mode="adaptive",
        request_budget=2,
        token_budget=100,
        embedding_upstream_url="https://openrouter.ai/api/v1/embeddings",
        embedding_model="perplexity/pplx-embed-v1-0.6b",
        embedding_profile="dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1",
        embedding_provider="Perplexity",
        embedding_dimensions=768,
        embedding_request_budget=100_000,
        embedding_token_budget=1_000_000_000,
        embedding_per_ticket_concurrency=1,
        embedding_per_validator_concurrency=8,
        embedding_global_concurrency=32,
        embedding_per_ticket_requests_per_minute=10_000,
        embedding_per_validator_requests_per_minute=40_000,
        embedding_global_requests_per_minute=100_000,
        embedding_request_body_bytes=1 << 20,
        embedding_response_body_bytes=16 << 20,
        per_ticket_concurrency=1,
        per_validator_concurrency=1,
        global_concurrency=1,
        per_ticket_requests_per_minute=2,
        per_validator_requests_per_minute=2,
        global_requests_per_minute=2,
        request_body_bytes=1024,
        response_body_bytes=1024,
        timeout_seconds=10,
        max_output_tokens=32,
    )


async def _live_grant(
    session: AsyncSession,
    config: InferenceProxyConfig | None = None,
    validator_hotkey: str = "validator",
):
    """Mint and activate one live grant.

    ``config`` is threaded through because the budgets that bind admission are
    the ones *stamped onto the grant row* at mint time, not the ones in the
    config handed to ``begin_inference_request``. A test that raises a budget
    only on the admission call is testing nothing -- see
    ``test_the_operator_budget_is_stamped_onto_the_new_grant``.
    """
    config = config if config is not None else _config()
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="miner",
        name="parallel-inference",
        sha256="ab" * 32,
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
    await _seed_calibrated_route(session, now)
    await session.flush()
    grant = await ensure_inference_grant(session, ticket=ticket, config=config)
    assert grant is not None
    assert (
        await activate_inference_grant(
            session,
            grant_id=grant.grant_id,
            validator_hotkey="wrong-validator",
            broker_public_key="broker-key",
            now=now,
            config=config,
        )
        is None
    )
    activated = await activate_inference_grant(
        session,
        grant_id=grant.grant_id,
        validator_hotkey=validator_hotkey,
        broker_public_key="broker-key",
        now=now,
        config=config,
    )
    assert activated is not None
    return ticket, activated[0], activated[1], now


@pytest.mark.asyncio
async def test_v7_grant_requires_and_binds_one_calibrated_dynamic_route(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    config = _config()
    async with session.begin():
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey="miner-v7",
            name="adaptive-route",
            sha256="cd" * 32,
            status=AgentStatus.EVALUATING,
            created_at=now,
        )
        ticket = ValidatorTicket(
            agent_id=agent.agent_id,
            validator_hotkey="validator-v7",
            slot_id="slot-0",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=20),
            bench_version=_BENCH_VERSION,
            attempt_count=1,
        )
        session.add_all([agent, ticket])
        await session.flush()
        assert (
            await ensure_inference_grant(session, ticket=ticket, config=config) is None
        )

        route = InferenceProviderRoute(
            model="openai/gpt-oss-20b",
            provider="discovered-provider",
            profile_revision="openrouter-route-test-v1",
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
        session.add(route)
        session.add(
            InferenceRoutingPolicy(
                model="openai/gpt-oss-20b",
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
        await session.flush()
        grant = await ensure_inference_grant(session, ticket=ticket, config=config)
        assert grant is not None
        assert grant.allowed_models == ["openai/gpt-oss-20b"]
        assert grant.route_provider == "discovered-provider"
        assert grant.route_profile == "openrouter-route-test-v1"
        assert route.selected_ticket_count == 1
        assert route.exploration_ticket_count == 1
        activated = await activate_inference_grant(
            session,
            grant_id=grant.grant_id,
            validator_hotkey=ticket.validator_hotkey,
            broker_public_key="broker-key",
            now=now,
            config=config,
        )
        assert activated is not None
        bearer = activated[1]
        nonce = uuid4()
        started = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            bearer=bearer,
            model=config.embedding_model,
            token_reservation=1_000_000,
            now=now,
            config=config,
            request_kind="embedding",
        )
        assert isinstance(started, tuple)
        request = started[1]
        assert await finish_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            generation=grant.generation,
            status="completed",
            prompt_tokens=250_000,
            completion_tokens=0,
            cost_microusd=1_000,
            usage_available=True,
            now=now,
            upstream_provider=config.embedding_provider,
            upstream_attempts=3,
        )
        assert grant.embedding_request_count == 1
        assert grant.embedding_tokens == 250_000
        assert grant.embedding_cost_microusd == 1_000
        assert grant.request_count == 0
        assert grant.prompt_tokens == 0
        assert request.upstream_attempts == 3


@pytest.mark.asyncio
async def test_grant_rejects_wrong_bearer_model_budget_and_replay(
    session: AsyncSession,
) -> None:
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session)
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer="stolen-sibling-bearer",
                model=_CHAT_MODEL,
                token_reservation=10,
                now=now,
                config=_config(),
            )
            is InferenceDecline.UNATTRIBUTED
        )
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model="not-allowed",
                token_reservation=10,
                now=now,
                config=_config(),
            )
            is InferenceDecline.MODEL_NOT_PERMITTED
        )
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=101,
                now=now,
                config=_config(),
            )
            is InferenceDecline.RESERVATION_TOO_LARGE
        )
        # ...and asking for more than the whole allowance did NOT spend it. The
        # accepted request below is the assertion that matters: a lease must
        # survive one oversized call.
        assert grant.status == "active"
        nonce = uuid4()
        accepted = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            bearer=bearer,
            model=_CHAT_MODEL,
            token_reservation=10,
            now=now,
            config=_config(),
        )
        assert accepted is not None
        await finish_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            generation=grant.generation,
            status="completed",
            prompt_tokens=3,
            completion_tokens=4,
            cost_microusd=5,
            usage_available=True,
            now=now,
        )
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=nonce,
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=10,
                now=now,
                config=_config(),
            )
            is InferenceDecline.NONCE_REPLAYED
        )


@pytest.mark.asyncio
async def test_canceled_or_expired_ticket_revokes_capability(
    session: AsyncSession,
) -> None:
    async with session.begin():
        ticket, grant, bearer, now = await _live_grant(session)
        await revoke_ticket_inference(session, ticket=ticket, now=now)
        ticket.status = TicketStatus.EXPIRED
        # Named, not anonymous: a dead lease is the one refusal a broker must
        # never retry, so it is the one that most needs to be tellable apart
        # from a lane that is merely full.
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=10,
                now=now,
                config=_config(),
            )
            is InferenceDecline.GRANT_REVOKED
        )


@pytest.mark.asyncio
async def test_revocation_cancels_inflight_and_missing_usage_charges_reservation(
    session: AsyncSession,
) -> None:
    async with session.begin():
        ticket, grant, bearer, now = await _live_grant(session)
        nonce = uuid4()
        assert await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            bearer=bearer,
            model=_CHAT_MODEL,
            token_reservation=10,
            now=now,
            config=_config(),
        )
        await revoke_ticket_inference(session, ticket=ticket, now=now)
        ticket.status = TicketStatus.EXPIRED
        assert not await finish_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            generation=grant.generation,
            status="completed",
            prompt_tokens=0,
            completion_tokens=0,
            cost_microusd=0,
            usage_available=False,
            now=now,
        )

    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session)
        nonce = uuid4()
        assert await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            bearer=bearer,
            model=_CHAT_MODEL,
            token_reservation=10,
            now=now,
            config=_config(),
        )
        assert not await finish_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            generation=grant.generation,
            status="completed",
            prompt_tokens=0,
            completion_tokens=0,
            cost_microusd=0,
            usage_available=False,
            now=now,
        )
        assert grant.prompt_tokens == 10


@pytest.mark.asyncio
async def test_ticket_request_rate_is_bounded_after_requests_finish(
    session: AsyncSession,
) -> None:
    config = replace(
        _config(),
        request_budget=10,
        per_ticket_requests_per_minute=1,
        per_validator_requests_per_minute=10,
        global_requests_per_minute=10,
    )
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session)
        first_nonce = uuid4()
        assert await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=first_nonce,
            bearer=bearer,
            model=_CHAT_MODEL,
            token_reservation=10,
            now=now,
            config=config,
        )
        assert await finish_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=first_nonce,
            generation=grant.generation,
            status="completed",
            prompt_tokens=2,
            completion_tokens=1,
            cost_microusd=0,
            usage_available=True,
            now=now,
        )
        # A per-minute rate ceiling on a perfectly healthy lease. This is the
        # exact event that used to be indistinguishable from a revoked grant,
        # and the reason `banblackycat` died with its lease still live.
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=10,
                now=now,
                config=config,
            )
            is InferenceDecline.AT_CAPACITY
        )


async def _live_v7_embedding_grant(session: AsyncSession, config: InferenceProxyConfig):
    """A live v7 grant whose embedding lane is admissible.

    The grant row is written directly rather than through
    ``ensure_inference_grant``, which for v7 additionally requires a calibrated
    ``InferenceProviderRoute``. The classifier under test never reads the route
    -- it reads the ticket's liveness and the lane's counters -- so that fixture
    would be setup no assertion here depends on.
    """
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="miner-embedding",
        name="embedding-lane",
        sha256="ef" * 32,
        status=AgentStatus.EVALUATING,
        created_at=now,
    )
    ticket = ValidatorTicket(
        agent_id=agent.agent_id,
        validator_hotkey="validator-embedding",
        slot_id="slot-0",
        status=TicketStatus.ISSUED,
        issued_at=now,
        deadline=now + timedelta(minutes=20),
        bench_version=_BENCH_VERSION,
        attempt_count=1,
    )
    session.add_all([agent, ticket])
    await session.flush()
    grant = InferenceGrant(
        grant_id=uuid4(),
        agent_id=agent.agent_id,
        bench_version=_BENCH_VERSION,
        validator_hotkey=ticket.validator_hotkey,
        slot_id=ticket.slot_id,
        ticket_deadline=ticket.deadline,
        status="pending",
        bearer_digest=None,
        broker_public_key=None,
        generation=0,
        allowed_models=[_CHAT_MODEL],
        route_provider="test-provider",
        route_profile="openrouter-route-test-v1",
        request_budget=config.request_budget,
        token_budget=config.token_budget,
        embedding_model=config.embedding_model,
        embedding_profile=config.embedding_profile,
        embedding_provider=config.embedding_provider,
        embedding_dimensions=config.embedding_dimensions,
        embedding_request_budget=config.embedding_request_budget,
        embedding_token_budget=config.embedding_token_budget,
        embedding_request_count=0,
        embedding_tokens=0,
        embedding_cost_microusd=0,
        embedding_active_requests=0,
        request_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost_microusd=0,
        active_requests=0,
        expires_at=ticket.deadline,
    )
    session.add(grant)
    await session.flush()
    activated = await activate_inference_grant(
        session,
        grant_id=grant.grant_id,
        validator_hotkey=ticket.validator_hotkey,
        broker_public_key="broker-key",
        now=now,
        config=config,
    )
    assert activated is not None
    return ticket, activated[0], activated[1], now


@pytest.mark.asyncio
async def test_full_embedding_lane_is_backpressure_not_a_lost_lease(
    session: AsyncSession,
) -> None:
    """A saturated lane and a revoked lease must not look alike.

    Both used to return ``None`` and both became a ``429``, which dittobench-api
    reads as "the platform took my ticket away" and answers by discarding the
    whole run. That was survivable only while the limit was a constant nobody
    could move. Now that an operator can lower it from backroom under a live
    run, the two cases have to be told apart or the emergency brake destroys
    every run it touches.
    """
    config = replace(_config(), embedding_per_ticket_concurrency=1)
    async with session.begin():
        _ticket, grant, bearer, now = await _live_v7_embedding_grant(session, config)
        first = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=uuid4(),
            bearer=bearer,
            model=config.embedding_model,
            token_reservation=10,
            now=now,
            config=config,
            request_kind="embedding",
        )
        assert isinstance(first, tuple)

        # Second concurrent embedding: the lane is full, the lease is healthy.
        second = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=uuid4(),
            bearer=bearer,
            model=config.embedding_model,
            token_reservation=10,
            now=now,
            config=config,
            request_kind="embedding",
        )
        assert second is InferenceDecline.AT_CAPACITY


@pytest.mark.asyncio
async def test_revoked_lease_still_fails_closed_on_the_embedding_lane(
    session: AsyncSession,
) -> None:
    """A dead ticket is still fatal on the embedding lane -- and now says so.

    The refusal moved from an anonymous ``None`` to a named ``GRANT_REVOKED``.
    What did *not* move is which class it belongs to: this is still terminal,
    still answered with ``429``, and still must never be retried. Naming a
    refusal is not the same as softening it.
    """
    config = replace(_config(), embedding_per_ticket_concurrency=8)
    async with session.begin():
        ticket, grant, bearer, now = await _live_v7_embedding_grant(session, config)
        ticket.status = TicketStatus.EXPIRED
        await session.flush()
        declined = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=uuid4(),
            bearer=bearer,
            model=config.embedding_model,
            token_reservation=10,
            now=now,
            config=config,
            request_kind="embedding",
        )
        assert declined is InferenceDecline.GRANT_REVOKED


@pytest.mark.asyncio
async def test_raised_per_ticket_limit_actually_admits_concurrent_embeddings(
    session: AsyncSession,
) -> None:
    """The point of the whole change, asserted end to end on the admission path.

    Embeddings are ~63% of a v7 run's inference requests and were admitted one
    at a time. This is the assertion that the limit is what was serialising
    them, and that raising it is not a no-op.
    """
    config = replace(_config(), embedding_per_ticket_concurrency=8)
    async with session.begin():
        _ticket, grant, bearer, now = await _live_v7_embedding_grant(session, config)
        admitted = 0
        for _ in range(8):
            reserved = await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=config.embedding_model,
                token_reservation=10,
                now=now,
                config=config,
                request_kind="embedding",
            )
            if isinstance(reserved, tuple):
                admitted += 1
        assert admitted == 8
        assert grant.embedding_active_requests == 8

        # And the ceiling still holds at the ninth.
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=config.embedding_model,
                token_reservation=10,
                now=now,
                config=config,
                request_kind="embedding",
            )
            is InferenceDecline.AT_CAPACITY
        )


@pytest.mark.asyncio
async def test_chat_capacity_refusal_is_retryable_not_fatal(
    session: AsyncSession,
) -> None:
    """Chat capacity is backpressure, and now says so.

    This test previously asserted the opposite -- that chat kept its historical
    anonymous ``None`` -> 429 -- on the reasoning that chat limits are boot-time
    constants and so cannot surprise a live run. The premise was true and the
    conclusion still wrong: a chat rail can fill under ordinary load with
    nothing wrong with the lease, and answering that with the code that means
    "your lease is dead" is what ended `banblackycat` while its ticket was
    still live.
    """
    config = replace(_config(), per_ticket_concurrency=1)
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session)
        assert isinstance(
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=10,
                now=now,
                config=config,
            ),
            tuple,
        )
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=10,
                now=now,
                config=config,
            )
            is InferenceDecline.AT_CAPACITY
        )


# A grant minted by `_live_grant` carries `_config().request_budget`, i.e. two
# chat requests. Admission reads that stamped column and not the config handed
# to it, so this is the number the budget tests below exhaust.
_LIVE_GRANT_REQUEST_BUDGET = 2


def _budget_only_config() -> InferenceProxyConfig:
    """A config where the *request budget* is the only limit that can bind.

    ``_config`` deliberately pins every rail to 1-2 so the capacity tests can
    trip them cheaply. That makes it useless for asserting anything about the
    budget, which is checked before them -- the lane would answer AT_CAPACITY
    on the second call and never reach the budget at all.

    Note what raising ``request_budget`` here would *not* do: nothing. The value
    that binds was copied onto the grant row at mint time.
    """
    return replace(
        _config(),
        request_budget=999,
        token_budget=10_000,
        per_ticket_concurrency=64,
        per_validator_concurrency=64,
        global_concurrency=64,
        per_ticket_requests_per_minute=1000,
        per_validator_requests_per_minute=1000,
        global_requests_per_minute=1000,
    )


@pytest.mark.asyncio
async def test_spent_budget_is_named_and_stays_named(session: AsyncSession) -> None:
    """Exhaustion is terminal, distinguishable, and *persistent*.

    Persistence is the half that is easy to miss. The refusal that trips the
    budget sets ``status = "exhausted"``, so every later call in the run takes
    the early status gate instead. If that gate did not also name the reason,
    the signal would exist for exactly one request and the entire tail of the
    run -- the part where a harness still has time to wind down and submit --
    would see an anonymous refusal again.
    """
    config = _budget_only_config()
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session)
        for _ in range(_LIVE_GRANT_REQUEST_BUDGET):
            assert isinstance(
                await begin_inference_request(
                    session,
                    grant_id=grant.grant_id,
                    nonce=uuid4(),
                    bearer=bearer,
                    model=_CHAT_MODEL,
                    token_reservation=10,
                    now=now,
                    config=config,
                ),
                tuple,
            )
        for _ in range(3):
            assert (
                await begin_inference_request(
                    session,
                    grant_id=grant.grant_id,
                    nonce=uuid4(),
                    bearer=bearer,
                    model=_CHAT_MODEL,
                    token_reservation=10,
                    now=now,
                    config=config,
                )
                is InferenceDecline.BUDGET_EXHAUSTED
            )
        assert grant.status == "exhausted"


@pytest.mark.asyncio
async def test_a_bad_bearer_learns_nothing_about_the_grant(
    session: AsyncSession,
) -> None:
    """Why the reason is named *after* the bearer comparison, never before.

    The status of a grant is information about somebody else's lease. A caller
    that cannot prove it holds this grant's bearer gets the anonymous refusal
    and learns nothing -- not that the grant exists, not that it was revoked,
    not that it ran out of budget. Ordering is what enforces that, so this test
    exists to fail if the status gate is ever hoisted above the digest check.
    """
    config = _budget_only_config()
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session)
        for _ in range(_LIVE_GRANT_REQUEST_BUDGET):
            assert isinstance(
                await begin_inference_request(
                    session,
                    grant_id=grant.grant_id,
                    nonce=uuid4(),
                    bearer=bearer,
                    model=_CHAT_MODEL,
                    token_reservation=10,
                    now=now,
                    config=config,
                ),
                tuple,
            )
        # The holder is told the budget is spent...
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=10,
                now=now,
                config=config,
            )
            is InferenceDecline.BUDGET_EXHAUSTED
        )
        # ...and an impostor is told nothing at all.
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer="not-the-bearer",
                model=_CHAT_MODEL,
                token_reservation=10,
                now=now,
                config=config,
            )
            is InferenceDecline.UNATTRIBUTED
        )


@pytest.mark.asyncio
async def test_the_operator_budget_is_stamped_onto_the_new_grant(
    session: AsyncSession,
) -> None:
    """The board reaches a lease through the mint, not through admission.

    This is the whole mechanism behind ``chat_request_budget`` being safe to
    change under a live subnet: the number is copied onto the grant row once,
    when the lease is created, and admission thereafter reads the row. A
    revision therefore governs the next lease and can never retroactively
    exhaust one already running.
    """
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="miner",
        name="budget-stamp",
        sha256="cd" * 32,
        status=AgentStatus.EVALUATING,
        created_at=now,
    )
    ticket = ValidatorTicket(
        agent_id=agent.agent_id,
        validator_hotkey="validator",
        slot_id="slot-0",
        status=TicketStatus.ISSUED,
        issued_at=now,
        deadline=now + timedelta(minutes=20),
        bench_version=_BENCH_VERSION,
        attempt_count=1,
    )
    session.add_all([agent, ticket])
    await _seed_calibrated_route(session, now)
    await session.flush()
    board = apply_settings(
        _config(),
        InferenceConcurrencySettings(chat_request_budget=7, chat_token_budget=7_000),
    )
    grant = await ensure_inference_grant(session, ticket=ticket, config=board)
    assert grant is not None
    assert grant.request_budget == 7
    # Both allowances travel the same way. The token budget is the one that was
    # boot-time-only until now, which is why raising the request budget from
    # backroom did not save the runs it was meant to save.
    assert grant.token_budget == 7_000


def _token_budget_config(token_budget: int) -> InferenceProxyConfig:
    """A config where the *token* budget is the only limit that can bind.

    Rails wide, request budget effectively unbounded, so that reaching a refusal
    proves something about the token allowance and nothing else.
    """
    return replace(
        _config(),
        request_budget=1_000_000,
        token_budget=token_budget,
        per_ticket_concurrency=8,
        per_validator_concurrency=64,
        global_concurrency=64,
        per_ticket_requests_per_minute=1_000_000,
        per_validator_requests_per_minute=1_000_000,
        global_requests_per_minute=1_000_000,
    )


@pytest.mark.asyncio
async def test_a_spent_token_budget_is_named_terminal_and_persistent(
    session: AsyncSession,
) -> None:
    """The defect that made #473 inert, in miniature.

    A grant whose token allowance is gone used to answer a bare ``None``, which
    the envelope renders as 4100 and the broker classifies as transient. It then
    retried at ~2.5/sec for two minutes and the run died as
    ``model_relay_unavailable`` -- 1009 declines on one lease, every one of them
    4100, with 1h21m still on the clock.

    Note the arithmetic that hid this from the exhaustion check in
    ``finish_inference_request``: that one fires on ``spent >= token_budget``,
    but a run stalls one *request* short of the line and never books the last
    call, so ``spent`` freezes below the budget forever. Nothing set the status,
    so nothing named the refusal, on any call for the rest of the run.
    """
    config = _token_budget_config(1_000)
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        for _ in range(4):
            accepted = await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=(nonce := uuid4()),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=250,
                now=now,
                config=config,
            )
            assert isinstance(accepted, tuple)
            await finish_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=nonce,
                generation=grant.generation,
                status="completed",
                prompt_tokens=200,
                completion_tokens=25,
                cost_microusd=1,
                usage_available=True,
                now=now,
            )
        # 900 booked against a 1,000 budget: a 250-token call no longer fits.
        assert grant.prompt_tokens + grant.completion_tokens == 900
        assert grant.status == "active"
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=250,
                now=now,
                config=config,
            )
            is InferenceDecline.TOKEN_BUDGET_EXHAUSTED
        )
        # Terminal, and recorded as terminal -- matching request-count
        # exhaustion, so the broker must not retry it.
        assert grant.status == "exhausted"
        # And it stays named -- as the *token* wall, not the request wall -- for
        # the whole tail of the run, which is the window in which a harness can
        # still wind down and submit what it has.
        for _ in range(3):
            assert (
                await begin_inference_request(
                    session,
                    grant_id=grant.grant_id,
                    nonce=uuid4(),
                    bearer=bearer,
                    model=_CHAT_MODEL,
                    token_reservation=250,
                    now=now,
                    config=config,
                )
                is InferenceDecline.TOKEN_BUDGET_EXHAUSTED
            )


@pytest.mark.asyncio
async def test_in_flight_reservations_alone_are_backpressure_not_exhaustion(
    session: AsyncSession,
) -> None:
    """Nothing spent, so nothing is over -- the reservations will settle.

    The old code answered this identically to a genuinely spent allowance (both
    were a bare ``None``), which meant a grant with plenty of headroom could be
    reported as dead purely because several calls happened to be in flight. It
    has to degrade to backpressure or a healthy run is thrown away.
    """
    config = _token_budget_config(1_000)
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        for _ in range(3):
            assert isinstance(
                await begin_inference_request(
                    session,
                    grant_id=grant.grant_id,
                    nonce=uuid4(),
                    bearer=bearer,
                    model=_CHAT_MODEL,
                    token_reservation=300,
                    now=now,
                    config=config,
                ),
                tuple,
            )
        # 900 reserved, 0 spent. A fourth call crosses only because of the three
        # in flight.
        assert grant.prompt_tokens + grant.completion_tokens == 0
        assert (
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=300,
                now=now,
                config=config,
            )
            is InferenceDecline.AT_CAPACITY
        )
        # Emphatically still alive: backpressure must not spend the lease.
        assert grant.status == "active"


@pytest.mark.asyncio
async def test_a_jupiter_profile_run_completes_at_the_shipped_budget(
    session: AsyncSession,
) -> None:
    """The whole point of the change, measured against the real numbers.

    Jupiter and KOTH_v7_1 issue ~1090 chat calls at ~10k tokens each -- ~10.9M
    for a complete run. Against the old 4,000,000 that run is refused from call
    ~400 onward, which is 36% of the way in: the remaining ~690 calls were the
    1009 declines observed on a live lease.

    Scaled down by 1000x so the test is a few hundred rows rather than a
    million, but the ratio that matters -- run demand vs. allowance -- is
    preserved exactly.
    """
    per_call_prompt, per_call_completion = 9_400, 600
    calls = 1_090
    demand = calls * (per_call_prompt + per_call_completion)
    assert demand == 10_900_000

    async def _drive(token_budget: int) -> tuple[int, object]:
        config = _token_budget_config(token_budget)
        _ticket, grant, bearer, now = await _live_grant(
            session, config, validator_hotkey=f"validator-{token_budget}"
        )
        completed = 0
        for _ in range(calls):
            got = await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=(nonce := uuid4()),
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=per_call_prompt + per_call_completion,
                now=now,
                config=config,
            )
            if isinstance(got, InferenceDecline):
                return completed, got
            await finish_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=nonce,
                generation=grant.generation,
                status="completed",
                prompt_tokens=per_call_prompt,
                completion_tokens=per_call_completion,
                cost_microusd=1,
                usage_available=True,
                now=now,
            )
            completed += 1
        return completed, None

    async with session.begin():
        # The old allowance: cut off well before the run ends...
        completed, decline = await _drive(4_000_000)
        assert completed == 400
        assert decline is InferenceDecline.TOKEN_BUDGET_EXHAUSTED

    async with session.begin():
        # ...and the shipped one: the run finishes.
        completed, decline = await _drive(DEFAULT_CHAT_TOKEN_BUDGET)
        assert completed == calls
        assert decline is None


@pytest.mark.asyncio
async def test_a_reclaimed_request_is_charged_the_estimate_not_the_byte_count(
    session: AsyncSession,
) -> None:
    """The over-charge was booked, not merely reserved.

    Every path that reclaims a request which never returned trusted usage --
    timeout, transport failure, revocation, the stale sweep -- charges
    ``reserved_tokens`` as ``prompt_tokens``. While that figure was
    ``max_tokens + len(body)``, a call whose body was 8KB was permanently
    booked ~8,000 tokens against the grant when it really cost ~2,000.

    This asserts the charge follows the reservation, so an honest reservation is
    an honest charge. The endpoint-level halves -- that the reservation is now
    ~1/4 of the byte count, and that the ceiling stayed a true bound -- live in
    ``test_inference.py``.
    """
    config = _token_budget_config(1_000_000)
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        nonce = uuid4()
        # What the old code would have reserved for an 8KB body vs. what the
        # estimate reserves for the same call.
        byte_bound, estimate = 8_192, 2_048
        assert isinstance(
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=nonce,
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=estimate,
                max_chargeable_tokens=byte_bound,
                now=now,
                config=config,
            ),
            tuple,
        )
        # The provider never answered, so the reservation is what gets booked.
        await finish_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            generation=grant.generation,
            status="failed",
            prompt_tokens=0,
            completion_tokens=0,
            cost_microusd=0,
            usage_available=False,
            now=now,
        )
        assert grant.prompt_tokens == estimate
        assert grant.prompt_tokens != byte_bound


@pytest.mark.asyncio
async def test_real_usage_above_the_estimate_is_still_deliverable(
    session: AsyncSession,
) -> None:
    """The half of this change that would break v7 if it were got wrong.

    ``finish_inference_request`` marks a request non-deliverable when reported
    usage exceeds its bound, and the endpoint turns that into a 409. Once the
    reservation is an estimate, ordinary token-dense prompts land above it --
    so clamping there would 409 a large share of perfectly good calls.

    The clamp therefore reads ``max_chargeable_tokens``, which is still the
    byte-derived true bound. Usage between the estimate and the ceiling is
    booked exactly as reported.
    """
    config = _token_budget_config(1_000_000)
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        nonce = uuid4()
        estimate, ceiling = 2_048, 8_192
        assert isinstance(
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=nonce,
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=estimate,
                max_chargeable_tokens=ceiling,
                now=now,
                config=config,
            ),
            tuple,
        )
        # 2,600 real tokens: over the estimate, well under the ceiling.
        assert await finish_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            generation=grant.generation,
            status="completed",
            prompt_tokens=2_400,
            completion_tokens=200,
            cost_microusd=7,
            usage_available=True,
            now=now,
        )
        assert grant.prompt_tokens == 2_400
        assert grant.completion_tokens == 200

    async with session.begin():
        # ...and a provider claiming more than the byte ceiling is still clamped.
        _ticket, grant, bearer, now = await _live_grant(
            session, config, validator_hotkey="validator-liar"
        )
        nonce = uuid4()
        assert isinstance(
            await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=nonce,
                bearer=bearer,
                model=_CHAT_MODEL,
                token_reservation=2_048,
                max_chargeable_tokens=8_192,
                now=now,
                config=config,
            ),
            tuple,
        )
        assert not await finish_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=nonce,
            generation=grant.generation,
            status="completed",
            prompt_tokens=9_000_000,
            completion_tokens=0,
            cost_microusd=1,
            usage_available=True,
            now=now,
        )
        assert grant.prompt_tokens == 8_192


@pytest.mark.asyncio
async def test_new_grants_record_which_meter_booked_them(
    session: AsyncSession,
) -> None:
    """A token total is only comparable within the contract that produced it.

    Without this marker, someone comparing a pre-fix run against a post-fix run
    concludes the agent got dramatically more efficient when only the meter
    changed. There is no backfill and there cannot be one: the over-charge
    happened at reservation time, so what those calls really consumed was never
    recorded.
    """
    async with session.begin():
        _ticket, grant, _bearer, _now = await _live_grant(session)
        assert grant.usage_accounting_version == USAGE_ACCOUNTING_VERSION
        assert USAGE_ACCOUNTING_VERSION == 2


def _at_capacity_count(lane: str, scope: str) -> float:
    """Current value of the admission backpressure counter for one gate.

    Counters are process-global and every test in the session shares them, so
    callers must diff around the action under test rather than assert absolute
    values.
    """
    return (
        REGISTRY.get_sample_value(
            "ditto_inference_admission_at_capacity_total",
            {"lane": lane, "scope": scope},
        )
        or 0.0
    )


@pytest.mark.asyncio
async def test_emergency_brake_records_which_gate_declined(
    session: AsyncSession,
) -> None:
    """Lowering the per-ticket ceiling must be *visible*, not just effective.

    The brake already worked; nothing exported the fact that it had engaged.
    That is how the embedding ceiling spent months suspected of throttling v7
    runs while never once binding -- the only way to check was to reconstruct
    in-flight intervals from ``inference_requests`` after the fact. An operator
    applying the brake should be able to watch it take hold on a scrape, and an
    operator wondering whether a ceiling is the problem should be able to see a
    flat zero and stop wondering.
    """
    config = replace(_config(), embedding_per_ticket_concurrency=1)
    before = _at_capacity_count("embedding", "per_ticket")
    async with session.begin():
        _ticket, grant, bearer, now = await _live_v7_embedding_grant(session, config)
        admitted = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=uuid4(),
            bearer=bearer,
            model=config.embedding_model,
            token_reservation=10,
            now=now,
            config=config,
            request_kind="embedding",
        )
        assert isinstance(admitted, tuple)

        declined = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=uuid4(),
            bearer=bearer,
            model=config.embedding_model,
            token_reservation=10,
            now=now,
            config=config,
            request_kind="embedding",
        )

    # The wire contract is unchanged: still one undifferentiated AT_CAPACITY,
    # because a broker's correct response to every gate is identical.
    assert declined is InferenceDecline.AT_CAPACITY
    # The operator's view is the one that gained resolution.
    assert _at_capacity_count("embedding", "per_ticket") == before + 1


@pytest.mark.asyncio
async def test_capacity_metric_separates_the_two_lanes(
    session: AsyncSession,
) -> None:
    """A chat decline must never be counted against the embedding ceiling.

    Both lanes share one admission function and one decline value, so without
    the ``lane`` label the counter would answer "is the embedding ceiling
    binding?" with chat's backpressure -- which is exactly the conflation that
    made the original diagnosis wrong.
    """
    config = replace(_config(), per_ticket_concurrency=1)
    before_chat = _at_capacity_count("chat", "per_ticket")
    before_embedding = _at_capacity_count("embedding", "per_ticket")
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        admitted = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=uuid4(),
            bearer=bearer,
            model=_CHAT_MODEL,
            token_reservation=1,
            now=now,
            config=config,
        )
        assert isinstance(admitted, tuple)
        declined = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=uuid4(),
            bearer=bearer,
            model=_CHAT_MODEL,
            token_reservation=1,
            now=now,
            config=config,
        )

    assert declined is InferenceDecline.AT_CAPACITY
    assert _at_capacity_count("chat", "per_ticket") == before_chat + 1
    assert _at_capacity_count("embedding", "per_ticket") == before_embedding


@pytest.mark.asyncio
async def test_global_ceiling_is_reported_as_global_not_as_per_ticket(
    session: AsyncSession,
) -> None:
    """The five wide gates used to share one anonymous ``or``.

    "The lane is full" and "the FLEET ceiling is full" are the same event to a
    validator and completely different events to the operator deciding which
    number to move. With a per-ticket allowance well above the global one, the
    global gate is the only one that can trip, and it has to say so.
    """
    config = replace(
        _config(),
        embedding_per_ticket_concurrency=8,
        embedding_per_validator_concurrency=8,
        embedding_global_concurrency=2,
    )
    before_global = _at_capacity_count("embedding", "global")
    before_per_ticket = _at_capacity_count("embedding", "per_ticket")
    async with session.begin():
        _ticket, grant, bearer, now = await _live_v7_embedding_grant(session, config)
        for _ in range(2):
            admitted = await begin_inference_request(
                session,
                grant_id=grant.grant_id,
                nonce=uuid4(),
                bearer=bearer,
                model=config.embedding_model,
                token_reservation=10,
                now=now,
                config=config,
                request_kind="embedding",
            )
            assert isinstance(admitted, tuple)

        declined = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=uuid4(),
            bearer=bearer,
            model=config.embedding_model,
            token_reservation=10,
            now=now,
            config=config,
            request_kind="embedding",
        )

    assert declined is InferenceDecline.AT_CAPACITY
    assert _at_capacity_count("embedding", "global") == before_global + 1
    assert _at_capacity_count("embedding", "per_ticket") == before_per_ticket


@pytest.mark.asyncio
async def test_lease_model_usage_reads_the_grant_bound_to_that_exact_lease(
    session: AsyncSession,
) -> None:
    """Usage resolves by lease identity, not by time proximity.

    The retrospective join people reach for -- newest grant for
    (agent, validator, bench_version) -- silently attributes a *later* run's
    spend to an earlier score. Keying on ticket_deadline as well makes the
    lookup exact.
    """
    async with session.begin():
        ticket, grant, _bearer, _now = await _live_grant(session)
        grant.request_count = 431
        grant.prompt_tokens = 1_160_122
        grant.completion_tokens = 92_400
        await session.flush()

        usage = await get_lease_model_usage(session, ticket=ticket)

    assert usage is not None
    assert usage.chat_calls == 431
    assert usage.prompt_tokens == 1_160_122
    assert usage.completion_tokens == 92_400
    assert usage.total_tokens == 1_252_522
    assert usage.accounting_version == USAGE_ACCOUNTING_VERSION


@pytest.mark.asyncio
async def test_lease_model_usage_is_none_when_no_grant_exists(
    session: AsyncSession,
) -> None:
    """No grant means *unknown*, never *unused*.

    A lease that ran with the inference proxy disabled produced no grant. It
    must not be reported as an agent that declined to call the model.
    """
    now = datetime.now(UTC)
    async with session.begin():
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey="miner-ungranted",
            name="no-proxy",
            sha256="ef" * 32,
            status=AgentStatus.EVALUATING,
            created_at=now,
        )
        ticket = ValidatorTicket(
            agent_id=agent.agent_id,
            validator_hotkey="validator",
            slot_id="slot-0",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=20),
            bench_version=7,
            attempt_count=1,
        )
        session.add_all([agent, ticket])
        await session.flush()

        assert await get_lease_model_usage(session, ticket=ticket) is None


@pytest.mark.asyncio
async def test_lease_model_usage_excludes_embedding_spend(
    session: AsyncSession,
) -> None:
    """Embeddings are retrieval, not model use.

    A retrieval-only agent embeds heavily -- production shows ~200 embedding
    calls against a single 74-token chat call. Folding embeddings into the
    total would erase precisely the signal this measurement exists to expose.
    """
    async with session.begin():
        ticket, grant, _bearer, _now = await _live_grant(session)
        grant.request_count = 1
        grant.prompt_tokens = 74
        grant.completion_tokens = 1
        grant.embedding_request_count = 204
        grant.embedding_tokens = 812_000
        await session.flush()

        usage = await get_lease_model_usage(session, ticket=ticket)

    assert usage is not None
    assert usage.total_tokens == 75
