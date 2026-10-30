"""Per-run cost ceiling (#3271): mint stamping, ledger settlement, admission.

Python mirror of the Go relay's ``cost_ceiling_pg_test.go``. The grant minted
by ``_live_grant`` is bound to the calibrated adaptive route seeded in
``test_inference``, priced at 3e-8 / 1.3e-7 USD per prompt / completion token,
so a 200 + 25 token call books ``round(6 + 3.25) = 9`` micro-USD of ledger cost
whatever the provider reports.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.inference_concurrency_settings import (
    InferenceConcurrencySettings,
)
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.config import InferenceProxyConfig
from ditto.api_server.inference_concurrency_settings import apply_settings
from ditto.api_server.inference_routing import (
    AGGREGATE_CALIBRATION_SAMPLES,
    aggregate_profile_revision,
)
from ditto.db.models import (
    Agent,
    AgentStatus,
    InferenceGrant,
    InferenceProviderRoute,
    InferenceRequest,
    ValidatorTicket,
)
from ditto.db.queries.inference import (
    InferenceDecline,
    begin_inference_request,
    ensure_inference_grant,
    finish_inference_request,
    ledger_cost_headers,
    ledger_cost_microusd,
    ticket_inference_grant_spent_allowance,
)
from ditto.tests.db.queries.test_inference import (
    _BENCH_VERSION,
    _CHAT_MODEL,
    _config,
    _live_grant,
    _seed_calibrated_route,
    _token_budget_config,
)

_LEDGER_PER_CALL = 9  # 200 * 3e-8 + 25 * 1.3e-7 USD, in micro-USD, rounded


def _ceiling_config(ceiling: int | None, mode: str) -> InferenceProxyConfig:
    return replace(
        _token_budget_config(1_000_000_000),
        run_cost_ceiling_microusd=ceiling,
        cost_ceiling_mode=mode,
    )


async def _chat(
    session: AsyncSession,
    *,
    grant: InferenceGrant,
    bearer: str,
    now: datetime,
    config: InferenceProxyConfig,
    cost_microusd: int = 5_000,
) -> InferenceDecline | None:
    """One admitted-and-settled chat call; returns the decline, if any."""
    nonce = uuid4()
    accepted = await begin_inference_request(
        session,
        grant_id=grant.grant_id,
        nonce=nonce,
        bearer=bearer,
        model=_CHAT_MODEL,
        token_reservation=250,
        now=now,
        config=config,
    )
    if isinstance(accepted, InferenceDecline):
        return accepted
    await finish_inference_request(
        session,
        grant_id=grant.grant_id,
        nonce=nonce,
        generation=grant.generation,
        status="completed",
        prompt_tokens=200,
        completion_tokens=25,
        cost_microusd=cost_microusd,
        usage_available=True,
        now=now,
    )
    return None


async def _embedding_admission(
    session: AsyncSession,
    *,
    grant: InferenceGrant,
    bearer: str,
    now: datetime,
    config: InferenceProxyConfig,
) -> InferenceDecline | None:
    accepted = await begin_inference_request(
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
    return accepted if isinstance(accepted, InferenceDecline) else None


def _would_decline(lane: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "ditto_inference_cost_ceiling_would_decline_total", {"lane": lane}
        )
        or 0.0
    )


@pytest.mark.asyncio
async def test_mint_stamps_ceiling_mode_and_list_prices(session: AsyncSession) -> None:
    settings = InferenceConcurrencySettings(
        run_cost_ceiling_microusd=2_500_000, cost_ceiling_mode="shadow"
    )
    config = apply_settings(_config(), settings)
    async with session.begin():
        _ticket, grant, _bearer, _now = await _live_grant(session, config)
        assert grant.cost_ceiling_microusd == 2_500_000
        assert grant.cost_ceiling_mode == "shadow"
        assert grant.cost_ceiling_would_decline == 0
        assert grant.ledger_cost_microusd == 0
        # The adaptive route's own prices become the ledger list prices.
        assert grant.ledger_prompt_price_per_token == pytest.approx(3e-8)
        assert grant.ledger_completion_price_per_token == pytest.approx(1.3e-7)
        assert grant.embedding_price_microusd_per_token == 0.004


@pytest.mark.asyncio
async def test_off_mode_mint_stamps_no_ceiling(session: AsyncSession) -> None:
    # A ceiling on the board in ``off`` mode is pre-sizing, not a live guard:
    # nothing is stamped, so the exchange and every response stay unchanged.
    settings = InferenceConcurrencySettings(run_cost_ceiling_microusd=2_500_000)
    config = apply_settings(_config(), settings)
    async with session.begin():
        _ticket, grant, _bearer, _now = await _live_grant(session, config)
        assert grant.cost_ceiling_microusd is None
        assert grant.cost_ceiling_mode == "off"
        assert ledger_cost_headers(grant) == {}


@pytest.mark.asyncio
async def test_aggregate_route_stamps_median_discovered_list_price(
    session: AsyncSession,
) -> None:
    """The aggregate route carries no price; the ledger must still have one.

    The median over the model's priced, non-offline provider endpoints is
    stamped, so the upstream's per-call provider choice never moves the charge.
    """
    now = datetime.now(UTC)
    async with session.begin():
        await _seed_calibrated_route(session, now)
        for provider, prompt, completion, status in (
            ("p-a", 1e-7, 4e-7, "healthy"),
            ("p-b", 2e-7, 5e-7, "discovered"),
            ("p-c", 9e-7, 9e-7, "offline"),  # ignored: offline
        ):
            session.add(
                InferenceProviderRoute(
                    model=_CHAT_MODEL,
                    provider=provider,
                    profile_revision=f"openrouter-route-{provider}-v1",
                    status=status,
                    calibration_status="shadow",
                    prompt_price_per_token=prompt,
                    completion_price_per_token=completion,
                    discovered_at=now,
                )
            )
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey="miner-aggregate",
            name="aggregate-ledger",
            sha256="cd" * 32,
            status=AgentStatus.EVALUATING,
            created_at=now,
        )
        ticket = ValidatorTicket(
            agent_id=agent.agent_id,
            validator_hotkey="validator-aggregate",
            slot_id="slot-0",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=20),
            bench_version=_BENCH_VERSION,
            attempt_count=1,
        )
        session.add_all([agent, ticket])
        await session.flush()
        # Unpriced aggregate route, which is what select_route binds in
        # aggregate_throughput mode.
        session.add(
            InferenceProviderRoute(
                model=_CHAT_MODEL,
                provider="openrouter",
                profile_revision=aggregate_profile_revision(
                    _CHAT_MODEL, bench_version=_BENCH_VERSION
                ),
                status="healthy",
                calibration_status="eligible",
                calibration_tool_accuracy=0.65,
                calibration_composite=0.20,
                calibration_sample_count=AGGREGATE_CALIBRATION_SAMPLES,
                calibration_manifest_sha256="ab" * 32,
                discovered_at=now,
            )
        )
        await session.flush()
        grant = await ensure_inference_grant(
            session,
            ticket=ticket,
            config=replace(_config(), routing_mode="aggregate_throughput"),
        )
        assert grant is not None
        assert grant.route_prompt_price_per_token is None
        # Median of the three non-offline priced endpoints (incl. the
        # calibrated fixture at 3e-8 / 1.3e-7).
        assert grant.ledger_prompt_price_per_token == pytest.approx(1e-7)
        assert grant.ledger_completion_price_per_token == pytest.approx(4e-7)


def test_ledger_cost_ignores_the_provider_bill_when_priced() -> None:
    grant = InferenceGrant(
        ledger_prompt_price_per_token=3e-8,
        ledger_completion_price_per_token=1.3e-7,
        embedding_price_microusd_per_token=0.004,
    )
    for provider_cost in (0, 9, 5_000, 1 << 40):
        assert (
            ledger_cost_microusd(
                grant,
                request_kind="chat",
                prompt_tokens=200,
                completion_tokens=25,
                provider_cost_microusd=provider_cost,
            )
            == _LEDGER_PER_CALL
        )
    # Embeddings at the default stamp are exactly the legacy catalog figure.
    for tokens in range(0, 3000):
        assert ledger_cost_microusd(
            grant,
            request_kind="embedding",
            prompt_tokens=tokens,
            completion_tokens=0,
            provider_cost_microusd=999,
        ) == round(tokens * 0.004)
    # An unpriced chat grant falls back to the (trusted) provider bill.
    grant.ledger_prompt_price_per_token = None
    assert (
        ledger_cost_microusd(
            grant,
            request_kind="chat",
            prompt_tokens=200,
            completion_tokens=25,
            provider_cost_microusd=5_000,
        )
        == 5_000
    )


@pytest.mark.asyncio
async def test_off_mode_books_ledger_and_changes_nothing_else(
    session: AsyncSession,
) -> None:
    config = _ceiling_config(None, "off")
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        for _ in range(3):
            assert (
                await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
                is None
            )
        assert grant.ledger_cost_microusd == 3 * _LEDGER_PER_CALL
        assert grant.cost_microusd == 3 * 5_000  # the provider bill, unchanged
        assert grant.status == "active"
        assert ledger_cost_headers(grant) == {}


@pytest.mark.asyncio
async def test_enforce_lets_the_crossing_call_finish_then_4110_on_both_lanes(
    session: AsyncSession,
) -> None:
    config = _ceiling_config(15, "enforce")
    async with session.begin():
        ticket, grant, bearer, now = await _live_grant(session, config)
        assert (
            await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
            is None
        )
        assert grant.ledger_cost_microusd == 9
        # 9 < 15: admitted, and this one crosses. It finishes and is billed.
        assert (
            await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
            is None
        )
        assert grant.ledger_cost_microusd == 18
        assert grant.status == "exhausted"
        assert ledger_cost_headers(grant) == {"X-Ditto-Ledger-Cost-Microusd": "18"}
        assert (
            await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
            is InferenceDecline.COST_CEILING_EXHAUSTED
        )
        assert (
            await _embedding_admission(
                session, grant=grant, bearer=bearer, now=now, config=config
            )
            is InferenceDecline.COST_CEILING_EXHAUSTED
        )
        # The platform's own meter treats the cost wall as allowance spent.
        assert await ticket_inference_grant_spent_allowance(
            session,
            agent_id=ticket.agent_id,
            bench_version=ticket.bench_version,
            validator_hotkey=ticket.validator_hotkey,
            ticket_deadline=ticket.deadline,
        )


@pytest.mark.asyncio
async def test_cost_wall_is_named_before_the_token_wall(session: AsyncSession) -> None:
    config = replace(_ceiling_config(5, "enforce"), token_budget=225)
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        assert (
            await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
            is None
        )
        # Both walls are now spent on settled usage; the dollar one names it.
        assert grant.prompt_tokens + grant.completion_tokens >= grant.token_budget
        assert grant.status == "exhausted"
        assert (
            await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
            is InferenceDecline.COST_CEILING_EXHAUSTED
        )


@pytest.mark.asyncio
async def test_shadow_admits_and_counts_would_decline(session: AsyncSession) -> None:
    config = _ceiling_config(5, "shadow")
    chat_before = _would_decline("chat")
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        for _ in range(3):
            assert (
                await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
                is None
            )
        assert grant.status == "active"
        # Calls 2 and 3 were admitted past a spent ceiling.
        assert grant.cost_ceiling_would_decline == 2
        assert grant.ledger_cost_microusd == 3 * _LEDGER_PER_CALL
    assert _would_decline("chat") - chat_before == 2


@pytest.mark.asyncio
async def test_null_ceiling_has_no_effect(session: AsyncSession) -> None:
    config = _ceiling_config(None, "enforce")
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        assert grant.cost_ceiling_microusd is None
        grant.ledger_cost_microusd = 10**12
        assert (
            await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
            is None
        )
        assert grant.status == "active"


@pytest.mark.asyncio
async def test_stale_reclamation_books_zero_ledger(session: AsyncSession) -> None:
    config = _ceiling_config(1_000, "enforce")
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        stale_nonce = uuid4()
        accepted = await begin_inference_request(
            session,
            grant_id=grant.grant_id,
            nonce=stale_nonce,
            bearer=bearer,
            model=_CHAT_MODEL,
            token_reservation=250,
            now=now,
            config=config,
        )
        assert isinstance(accepted, tuple)
        later = now + timedelta(seconds=config.timeout_seconds * 3)
        assert (
            await _chat(session, grant=grant, bearer=bearer, now=later, config=config)
            is None
        )
        stale = await session.get(InferenceRequest, (grant.grant_id, stale_nonce))
        assert stale is not None
        assert stale.status == "canceled"
        assert stale.ledger_cost_microusd == 0
        assert grant.ledger_cost_microusd == _LEDGER_PER_CALL


@pytest.mark.asyncio
async def test_synthetic_third_lane_joins_by_booking_ledger(
    session: AsyncSession,
) -> None:
    """A lane admission knows nothing about joins the ceiling purely by
    adding to ``ledger_cost_microusd`` at its own settlement."""
    config = _ceiling_config(100, "enforce")
    async with session.begin():
        _ticket, grant, bearer, now = await _live_grant(session, config)
        await session.execute(
            update(InferenceGrant)
            .where(InferenceGrant.grant_id == grant.grant_id)
            .values(ledger_cost_microusd=InferenceGrant.ledger_cost_microusd + 100)
        )
        await session.refresh(grant)
        assert grant.prompt_tokens + grant.completion_tokens == 0
        assert grant.embedding_tokens == 0
        assert (
            await _chat(session, grant=grant, bearer=bearer, now=now, config=config)
            is InferenceDecline.COST_CEILING_EXHAUSTED
        )
        assert (
            await _embedding_admission(
                session, grant=grant, bearer=bearer, now=now, config=config
            )
            is InferenceDecline.COST_CEILING_EXHAUSTED
        )


def test_decision_lane_ledger_is_exact_half_even_per_million() -> None:
    grant = InferenceGrant(decision_input_microusd_per_mtok=42_000)

    def ledger(tokens: int) -> int:
        return ledger_cost_microusd(
            grant,
            request_kind="decision",
            prompt_tokens=tokens,
            completion_tokens=999,  # output is unbilled
            provider_cost_microusd=7_777,  # never the bill
        )

    assert ledger(0) == 0
    assert ledger(1_000_000) == 42_000
    assert ledger(25) == 1  # 1.05
    grant.decision_input_microusd_per_mtok = 500_000
    assert ledger(1) == 0  # 0.5 -> even
    assert ledger(3) == 2  # 1.5 -> even
