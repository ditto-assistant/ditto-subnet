"""Heartbeats committed during awaited work use the completed read's clock."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bittensor_wallet import Keypair
from fastapi import HTTPException

from ditto.api_models import LedgerResponse
from ditto.api_server import treasury_weights
from ditto.api_server.endpoints.scoring import (
    _LedgerSnapshot,
    _require_statistical_cap_requester,
)
from ditto.api_server.ledger_pin import LedgerPinMaterializer
from ditto.chain.models import EpochSchedule
from ditto.db.models import ValidatorHeartbeat
from ditto.db.queries.ledger_epochs import get_pin
from ditto.tests.api_server.test_treasury_weights import add_runtime, app_state, pin
from ditto_screening_protocol.treasury import TreasuryLedgerPin


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 10, 7, 15, 36, 31, tzinfo=UTC))

    class ReadClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return clock.now

    monkeypatch.setattr(treasury_weights, "datetime", ReadClock)
    return clock


@pytest.mark.parametrize("heartbeat_during_build", [False, True])
async def test_heartbeat_during_build_does_not_block_epoch_pin(
    session, session_maker, monkeypatch, clock, heartbeat_during_build
):
    started = clock.now
    approved = pin()
    await add_runtime(session, started - timedelta(seconds=1))
    await session.commit()

    state = app_state(approved)
    state.config.treasury_shadow_policy = approved.policy
    state.config.treasury_weight_enforcement = True
    state.chain.get_treasury_collector_pin = AsyncMock(
        return_value=TreasuryLedgerPin(
            policy=approved.policy,
            policy_digest=approved.policy_digest,
            identity=approved.identity,
        )
    )
    schedule = EpochSchedule(
        netuid=118,
        subnet_epoch_index=approved.epoch_index,
        last_epoch_block=approved.first_block,
        pending_epoch_at=0,
        tempo=360,
        blocks_since_last_step=1,
        block=approved.pinned_block,
        block_hash=approved.pinned_block_hash,
        block_timestamp=int(started.timestamp()),
        next_epoch_block=approved.first_block + 360,
    )

    async def materialize(*_args, **_kwargs):
        if heartbeat_during_build:
            # An ordinary heartbeat request commits after the build begins.
            clock.now = started + timedelta(seconds=1)
            async with session_maker() as concurrent, concurrent.begin():
                member = await concurrent.get(
                    ValidatorHeartbeat, approved.fleet[0].validator_hotkey
                )
                member.seen_at = clock.now
        clock.now = started + timedelta(seconds=2)
        return _LedgerSnapshot(
            entries=[], generated_at=started, active_bench_version=13
        )

    monkeypatch.setattr(
        "ditto.api_server.endpoints.scoring.resolve_ledger_context", AsyncMock()
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.scoring.materialize_ledger_snapshot", materialize
    )
    result = await LedgerPinMaterializer().ensure(
        state, session_maker, schedule=schedule, now=started
    )
    assert result is not None, "A fresh heartbeat must not abort the entire pin"
    assert result.epoch_index == schedule.subnet_epoch_index
    # The freshness clock must not rewrite the snapshot's provenance timestamp.
    assert result.pinned_at == started
    async with session_maker() as verify:
        assert (
            await get_pin(verify, netuid=118, epoch_index=schedule.subnet_epoch_index)
            is not None
        )


@pytest.mark.parametrize("requester", ["managed", "follower"])
@pytest.mark.parametrize("fault", ["refresh", "expires_during_read", "future"])
async def test_ledger_delivery_uses_clock_after_each_heartbeat_read(
    session, session_maker, monkeypatch, clock, requester, fault
):
    started = clock.now
    approved = pin()
    managed = await add_runtime(session, started)
    hotkey = managed.validator_hotkey
    if requester == "follower":
        hotkey = Keypair.create_from_uri("//Dave").ss58_address
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=hotkey,
                protocol_version=30,
                software_version="0.356.0",
                seen_at=started,
                reported_at=started,
                signature="cd" * 64,
                capabilities={},
                state="idle",
                active_agent_id=None,
                code_digest="b" * 64,
            )
        )
    await session.commit()
    state = app_state(approved)
    state.config.treasury_weight_enforcement = True
    ledger = LedgerResponse(
        entries=[],
        count=0,
        generated_at=started,
        active_bench_version=13,
        treasury_pin=approved,
    )

    async with session_maker() as consumer:
        original_scalars = consumer.scalars
        original_get = consumer.get
        read_count = 0

        async def complete_read(read, *args, **kwargs):
            nonlocal read_count
            read_count += 1
            seen_at = {
                "refresh": started + timedelta(seconds=1),
                "expires_during_read": started
                - timedelta(minutes=15)
                + timedelta(seconds=1),
                "future": started + timedelta(seconds=3),
            }[fault]
            async with session_maker() as concurrent, concurrent.begin():
                row = await concurrent.get(ValidatorHeartbeat, hotkey)
                row.seen_at = seen_at
            result = await read(*args, **kwargs)
            # Sample only after the actual Postgres read completes. Sampling
            # just before the query still rejects the concurrent refresh.
            clock.now = started + timedelta(seconds=2)
            return result

        async def scalars(statement, **kwargs):
            if (
                requester == "managed"
                and statement.column_descriptions[0].get("entity") is ValidatorHeartbeat
            ):
                return await complete_read(original_scalars, statement, **kwargs)
            return await original_scalars(statement, **kwargs)

        async def get(model, key, **kwargs):
            if (
                requester == "follower"
                and model is ValidatorHeartbeat
                and key == hotkey
            ):
                return await complete_read(original_get, model, key, **kwargs)
            return await original_get(model, key, **kwargs)

        monkeypatch.setattr(consumer, "scalars", scalars)
        monkeypatch.setattr(consumer, "get", get)
        if fault == "refresh":
            result = await _require_statistical_cap_requester(
                consumer, hotkey, ledger, now=started, app_state=state
            )
            assert result is ledger
        else:
            with pytest.raises(HTTPException) as refused:
                await _require_statistical_cap_requester(
                    consumer, hotkey, ledger, now=started, app_state=state
                )
            assert refused.value.status_code == 428
            assert refused.value.detail == "treasury weight-setting fleet not ready"
        assert read_count == 1
