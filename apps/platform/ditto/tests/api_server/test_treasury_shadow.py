from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ditto.api_server.config import parse_treasury_shadow_policy
from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.ledger_pin import LedgerPin, ledger_digest
from ditto.api_server.treasury_shadow import observe_shadow_treasury, shadow_readiness
from ditto_screening_protocol.treasury import TreasuryLedgerPin

FIXTURE = (
    Path(__file__).resolve().parents[5]
    / "packages/ditto-screening-protocol/tests/fixtures"
    / "treasury_ledger_pin_v1.json"
)


def pin():
    return TreasuryLedgerPin.model_validate_json(FIXTURE.read_text())


def state(policy=None):
    return SimpleNamespace(
        config=SimpleNamespace(treasury_shadow_policy=policy),
        chain=SimpleNamespace(get_treasury_collector_pin=AsyncMock(return_value=pin())),
    )


def schedule():
    return SimpleNamespace(
        netuid=118,
        last_epoch_block=100,
        block=101,
        block_hash=pin().identity.finalized_block_hash,
    )


@pytest.mark.asyncio
async def test_disabled_producer_never_reads_collector_chain():
    app = state()
    assert await observe_shadow_treasury(app, schedule()) is None
    app.chain.get_treasury_collector_pin.assert_not_called()
    assert app.treasury_shadow_observer_status == "disabled"


@pytest.mark.asyncio
async def test_configured_proposal_gets_epoch_bound_shadow_observation():
    app = state(pin().policy)
    result = await observe_shadow_treasury(app, schedule())
    assert result == pin()
    assert result.mode == "shadow"
    app.chain.get_treasury_collector_pin.assert_awaited_once_with(
        pin().policy,
        first_block=100,
        pinned_block=101,
    )
    assert app.treasury_shadow_observer_status == "observed"


@pytest.mark.parametrize(
    "fault", ["timeout", "wrong_policy", "wrong_epoch", "wrong_hash"]
)
@pytest.mark.asyncio
async def test_failed_observation_cannot_be_substituted_or_marked_observed(fault):
    app = state(pin().policy)
    epoch = schedule()
    if fault == "timeout":
        app.chain.get_treasury_collector_pin.side_effect = TimeoutError()
    elif fault == "wrong_epoch":
        epoch.last_epoch_block = 102
    elif fault == "wrong_hash":
        epoch.block_hash = "0x" + "22" * 32
    else:
        raw = pin().policy.model_dump(mode="json")
        raw["revision"] += 1
        app.config.treasury_shadow_policy = type(pin().policy).model_validate(raw)
    with pytest.raises((ValueError, TimeoutError)):
        await observe_shadow_treasury(app, epoch)
    assert app.treasury_shadow_observer_status == "unavailable"


def test_default_config_has_no_proposal(monkeypatch):
    monkeypatch.delenv("DITTO_TREASURY_SHADOW_POLICY_JSON", raising=False)
    assert parse_treasury_shadow_policy() is None


def test_public_proposal_roundtrips_without_enforcement(monkeypatch):
    monkeypatch.setenv(
        "DITTO_TREASURY_SHADOW_POLICY_JSON", pin().policy.model_dump_json()
    )
    assert parse_treasury_shadow_policy() == pin().policy


@pytest.mark.parametrize("raw", ["not json", "{}", "x" * 8193])
def test_invalid_config_refuses_boot_without_echoing_input(monkeypatch, raw):
    monkeypatch.setenv("DITTO_TREASURY_SHADOW_POLICY_JSON", raw)
    with pytest.raises(ApiServerConfigError) as error:
        parse_treasury_shadow_policy()
    assert raw not in str(error.value)


def stored_pin():
    served = {"treasury_pin": pin().model_dump(mode="json")}
    return LedgerPin(
        netuid=118,
        epoch_index=7,
        last_epoch_block=100,
        pinned_block=101,
        pinned_block_hash=pin().identity.finalized_block_hash,
        pinned_at=datetime(2026, 10, 1, tzinfo=UTC),
        bench_version=14,
        entries=(),
        context={"served": served},
        ledger_digest=ledger_digest([], served),
    )


def test_readiness_missing_pin_is_disabled_without_chain_access():
    app = state()
    result = shadow_readiness(app, None)
    assert result.observer_status == "disabled"
    assert result.stored_shadow_pin is None
    assert result.latest_stored_epoch_index is None
    assert "producer_disabled" in result.blocking_reasons
    assert "no_epoch_pin" in result.blocking_reasons
    app.chain.get_treasury_collector_pin.assert_not_called()


def test_readiness_stored_observation_never_attests_funding_or_freshness():
    app = state(pin().policy)
    result = shadow_readiness(app, stored_pin())
    assert result.stored_shadow_pin == pin()
    assert result.latest_stored_epoch_index == 7
    assert result.observer_scope == "this_platform_process"
    assert result.observer_status == "not_observed"
    assert result.offline_policy_verified is False
    assert result.can_enforce_weights is False
    assert result.weight_effect == "none"
    assert set(result.blocking_reasons) == {
        "shadow_only",
        "offline_policy_unverified",
        "weight_adapter_not_active",
        "current_epoch_not_checked",
    }
    app.chain.get_treasury_collector_pin.assert_not_called()


@pytest.mark.parametrize(
    "fault", ["digest", "null_pin", "null_served", "list_served", "list_context"]
)
@pytest.mark.parametrize("projection", [True, False])
def test_readiness_corrupt_stored_evidence_is_bounded_and_unavailable(
    fault, projection
):
    stored = stored_pin()
    if fault == "digest":
        stored = replace(stored, ledger_digest="00" * 32)
    elif fault == "null_pin":
        stored = replace(stored, context={"served": {"treasury_pin": None}})
    elif fault == "null_served":
        stored = replace(stored, context={"served": None})
    elif fault == "list_served":
        stored = replace(stored, context={"served": []})
    else:
        stored = replace(stored, context=[])
    if not projection:
        stored = SimpleNamespace(**vars(stored))
    result = shadow_readiness(state(pin().policy), stored)
    assert result.latest_stored_epoch_index == 7
    assert result.latest_stored_ledger_digest == stored.ledger_digest
    assert result.stored_shadow_pin is None
    assert "stored_pin_invalid" in result.blocking_reasons
    assert result.can_enforce_weights is False


@pytest.mark.parametrize("status", ["future_state", [], None])
def test_readiness_unknown_process_status_is_unavailable(status):
    app = state(pin().policy)
    app.treasury_shadow_observer_status = status
    result = shadow_readiness(app, None)
    assert result.observer_status == "unavailable"
    assert result.can_enforce_weights is False


def test_readiness_legacy_object_without_treasury_pin_remains_absent():
    stored = replace(stored_pin(), context={"served": {}})
    result = shadow_readiness(state(pin().policy), stored)
    assert result.stored_shadow_pin is None
    assert "no_epoch_pin" in result.blocking_reasons
    assert "stored_pin_invalid" not in result.blocking_reasons


def test_readiness_current_proposal_drift_does_not_rewrite_stored_policy():
    raw = pin().policy.model_dump(mode="json")
    raw["revision"] += 1
    result = shadow_readiness(
        state(type(pin().policy).model_validate(raw)), stored_pin()
    )
    assert result.stored_shadow_pin == pin()
    assert result.configured_proposal.revision == pin().policy.revision + 1
    assert "proposal_pin_mismatch" in result.blocking_reasons
    assert result.can_enforce_weights is False
