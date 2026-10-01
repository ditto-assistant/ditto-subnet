"""Complete signed runtime fleet contract; synthetic public policy only."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from ditto.api_server.treasury_weights import (
    enforcing_pin_from_observation,
    read_treasury_fleet,
    require_enforcing_requester,
    treasury_fleet_members,
)
from ditto.db.models import ValidatorHeartbeat
from ditto_screening_protocol.treasury import TreasuryLedgerPin
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin
from ditto_screening_protocol.treasury_identity import TreasuryDispatchObservation


def pin():
    path = (
        Path(__file__).resolve().parents[5]
        / "packages/ditto-screening-protocol/tests/fixtures"
        / "treasury_enforcing_pin_v2.json"
    )
    return EnforcingTreasuryPin.model_validate_json(path.read_text())


def heartbeat(now):
    member = pin().fleet[0]
    return SimpleNamespace(
        validator_hotkey=member.validator_hotkey,
        protocol_version=30,
        seen_at=now,
        capabilities={
            "screened_images": True,
            "require_screened_image": True,
            "source_build_fallback": False,
            "full_stack_managed": False,
            "stack_updater": False,
            "sandbox_egress_restricted": False,
            "executor_isolation": "unknown",
            "treasury_weights": member.model_dump(
                exclude={"validator_hotkey", "protocol_version"}
            ),
        },
    )


def projection(rows, now):
    p = pin()
    return treasury_fleet_members(
        rows,
        now=now,
        policy_digest=p.policy_digest,
        collector_digest=p.policy.collector_policy_digest,
    )


def test_unscored_weight_setter_participates_without_scorer():
    now = datetime.now(UTC)
    assert projection([heartbeat(now)], now) == pin().fleet


@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "legacy",
        "missing_guard",
        "wrong_policy",
        "wrong_collector",
        "stale",
        "future",
        "duplicate",
    ],
)
def test_mixed_or_unproved_fleet_refuses(fault):
    now = datetime.now(UTC)
    row = heartbeat(now)
    rows = [row]
    if fault == "empty":
        rows = []
    elif fault == "legacy":
        row.protocol_version = 29
    elif fault == "missing_guard":
        row.capabilities = {}
    elif fault == "wrong_policy":
        row.capabilities["treasury_weights"]["approved_policy_digest"] = "f" * 64
    elif fault == "wrong_collector":
        row.capabilities["treasury_weights"]["collector_policy_digest"] = "f" * 64
    elif fault == "stale":
        row.seen_at -= timedelta(minutes=16)
    elif fault == "future":
        row.seen_at += timedelta(seconds=1)
    else:
        rows.append(row)
    with pytest.raises(ValueError):
        projection(rows, now)


async def test_stale_or_absent_chain_permitted_weight_setter_blocks_real_pg_fleet(
    session,
):
    now = datetime.now(UTC)
    row = heartbeat(now)
    session.add(
        ValidatorHeartbeat(
            validator_hotkey=row.validator_hotkey,
            protocol_version=30,
            software_version="0.337.0",
            seen_at=now,
            reported_at=now,
            signature="ab" * 64,
            capabilities=row.capabilities,
            state="idle",
            active_agent_id=None,
            code_digest="a" * 64,
        )
    )
    await session.flush()
    p = pin()
    args = {
        "now": now,
        "policy_digest": p.policy_digest,
        "collector_digest": p.policy.collector_policy_digest,
    }
    assert (
        await read_treasury_fleet(
            session, **args, required_hotkeys=(row.validator_hotkey,)
        )
        == p.fleet
    )
    with pytest.raises(ValueError):
        await read_treasury_fleet(
            session,
            **args,
            required_hotkeys=(row.validator_hotkey, p.policy.collector_hotkey),
        )
    dbrow = await session.get(ValidatorHeartbeat, row.validator_hotkey)
    dbrow.seen_at = now - timedelta(minutes=16)
    await session.flush()
    with pytest.raises(ValueError):
        await read_treasury_fleet(
            session, **args, required_hotkeys=(row.validator_hotkey,)
        )


def app_state(p):
    observation = TreasuryDispatchObservation(
        identity=p.identity,
        epoch_index=p.epoch_index,
        first_block=p.first_block,
        finalized_block=p.pinned_block,
        finalized_block_hash=p.pinned_block_hash,
    )
    return SimpleNamespace(
        config=SimpleNamespace(
            treasury_shadow_approval=p.approval,
            treasury_approved_policy_digest=p.policy_digest,
            treasury_approved_collector_policy_digest=p.policy.collector_policy_digest,
            chain=SimpleNamespace(netuid=p.policy.netuid),
        ),
        chain=SimpleNamespace(
            get_treasury_dispatch_observation=AsyncMock(return_value=observation),
            get_treasury_weight_setters=AsyncMock(
                return_value=tuple(m.validator_hotkey for m in p.fleet)
            ),
        ),
    )


async def add_runtime(session, now):
    row = heartbeat(now)
    dbrow = ValidatorHeartbeat(
        validator_hotkey=row.validator_hotkey,
        protocol_version=30,
        software_version="0.337.0",
        seen_at=now,
        reported_at=now,
        signature="ab" * 64,
        capabilities=row.capabilities,
        state="idle",
        active_agent_id=None,
        code_digest="a" * 64,
    )
    session.add(dbrow)
    await session.flush()
    return dbrow


async def test_producer_binds_real_approval_complete_roster_and_epoch(session):
    p = pin()
    now = datetime.now(UTC)
    await add_runtime(session, now)
    state = app_state(p)
    shadow = TreasuryLedgerPin(
        policy=p.policy, policy_digest=p.policy_digest, identity=p.identity
    )
    schedule = SimpleNamespace(
        subnet_epoch_index=p.epoch_index,
        last_epoch_block=p.first_block,
        block=p.pinned_block,
        block_hash=p.pinned_block_hash,
    )
    assert (
        await enforcing_pin_from_observation(state, session, shadow, schedule, now=now)
        == p
    )
    state.chain.get_treasury_weight_setters.assert_awaited_once_with(
        p.policy, block_hash=p.identity.finalized_block_hash
    )
    # A chain-active setter without a signed fresh runtime must halt pinning.
    state.chain.get_treasury_weight_setters.return_value += (p.policy.collector_hotkey,)
    with pytest.raises(ValueError, match="no fresh proof"):
        await enforcing_pin_from_observation(state, session, shadow, schedule, now=now)


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "stale",
        "legacy",
        "unlisted_requester",
        "new_setter",
        "missing_roster",
        "epoch_rollover",
        "owner_drift",
        "uid_reuse",
        "changed_deployment_pin",
        "rpc_failure",
    ],
)
async def test_requester_revalidates_current_chain_and_every_pinned_member(
    session, fault
):
    p = pin()
    now = datetime.now(UTC)
    row = await add_runtime(session, now)
    state = app_state(p)
    hotkey = row.validator_hotkey
    observation = state.chain.get_treasury_dispatch_observation.return_value
    if fault == "stale":
        row.seen_at -= timedelta(minutes=16)
    elif fault == "legacy":
        row.protocol_version = 29
    elif fault == "unlisted_requester":
        hotkey = p.policy.collector_hotkey
    elif fault == "new_setter":
        state.chain.get_treasury_weight_setters.return_value += (
            p.policy.collector_hotkey,
        )
    elif fault == "missing_roster":
        state.chain.get_treasury_weight_setters.return_value = ()
    elif fault == "epoch_rollover":
        state.chain.get_treasury_dispatch_observation.return_value = (
            observation.model_copy(update={"epoch_index": observation.epoch_index + 1})
        )
    elif fault in ("owner_drift", "uid_reuse"):
        identity_changes = (
            {"owner_coldkey": p.fleet[0].validator_hotkey}
            if fault == "owner_drift"
            else {"uid": p.identity.uid + 1}
        )
        state.chain.get_treasury_dispatch_observation.return_value = (
            observation.model_copy(
                update={"identity": p.identity.model_copy(update=identity_changes)}
            )
        )
    elif fault == "changed_deployment_pin":
        state.config.treasury_approved_policy_digest = "f" * 64
    elif fault == "rpc_failure":
        state.chain.get_treasury_dispatch_observation.side_effect = TimeoutError()
    await session.flush()
    if fault == "none":
        await require_enforcing_requester(session, p, hotkey, now=now, app_state=state)
        state.chain.get_treasury_weight_setters.assert_awaited_once_with(
            p.policy, block_hash=observation.finalized_block_hash
        )
    else:
        with pytest.raises((ValueError, TimeoutError)):
            await require_enforcing_requester(
                session, p, hotkey, now=now, app_state=state
            )


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("burn", [0, 0.5, 1])
def test_backend_prescribed_vector_keeps_service_pool_when_miners_burn(empty, burn):
    from ditto.api_server.ledger_pin import (
        LedgerPin,
        canonical_entries,
        ledger_digest,
        pin_expected_treasury_vector,
    )
    from ditto.tests.api_server.test_ledger_pin import _entry

    p = pin()
    now = datetime.now(UTC)
    miner = p.fleet[0].validator_hotkey
    entries = () if empty else (_entry(miner, 0.8, first_seen=now),)
    served = {"treasury_pin": p.model_dump(mode="json"), "burn_share": burn}
    ledger = LedgerPin(
        netuid=118,
        epoch_index=p.epoch_index,
        last_epoch_block=p.first_block,
        pinned_block=p.pinned_block,
        pinned_block_hash=p.pinned_block_hash,
        pinned_at=now,
        bench_version=13,
        entries=entries,
        context={"served": served},
        ledger_digest=ledger_digest(canonical_entries(entries), served),
    )
    vector = pin_expected_treasury_vector(ledger, burn_hotkey="burn")
    assert vector[p.policy.collector_hotkey] == 0.1
    expected_miner = 0 if empty else 0.9 * (1 - burn)
    assert vector.get(miner, 0) == pytest.approx(expected_miner)
    assert vector.get("burn", 0) == pytest.approx(0.9 - expected_miner)
    assert sum(vector.values()) == pytest.approx(1)


@pytest.mark.parametrize(
    "configured,has_v2", [(True, False), (False, True), (None, True)]
)
def test_cached_ledger_refuses_legacy_fallback_when_enforcing(configured, has_v2):
    from ditto.api_server.endpoints.scoring import _serve_last_known

    state = SimpleNamespace(
        ledger_snapshot=SimpleNamespace(treasury_pin=pin() if has_v2 else None),
    )
    if configured is not None:
        state.config = SimpleNamespace(treasury_weight_enforcement=configured)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    with pytest.raises(HTTPException) as refused:
        _serve_last_known(
            request, pin().fleet[0].validator_hotkey, RuntimeError("db down")
        )
    assert refused.value.status_code == 503
