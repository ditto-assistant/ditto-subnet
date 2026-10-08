"""Availability, authority rejection and safe operator evidence stay distinct."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from ditto.api_server import treasury_weights
from ditto.api_server.endpoints import scoring
from ditto.api_server.ledger_pin import LedgerPinMaterializer
from ditto.api_server.middleware.request_id import request_id_var
from ditto.api_server.treasury_read_diagnostics import record_treasury_read
from ditto.chain.errors import (
    ChainConnectionError,
    ChainTimeoutError,
    ChainTreasuryActivationReadError,
    ChainTreasuryReadTimeoutError,
)
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin


@pytest.mark.asyncio
async def test_schedule_failure_remains_visible_after_success(caplog):
    state = SimpleNamespace(
        chain=SimpleNamespace(
            read_epoch_schedule=AsyncMock(
                side_effect=[ChainTimeoutError("PRIVATE provider URL"), "schedule"]
            )
        ),
        config=SimpleNamespace(chain=SimpleNamespace(netuid=118)),
    )
    materializer = LedgerPinMaterializer()
    token = request_id_var.set("schedule-correlated")
    try:
        failed = await materializer.inspect_schedule(state)
        succeeded = await materializer.inspect_schedule(state)
    finally:
        request_id_var.reset(token)
    assert failed.failure_kind == "timeout"
    assert succeeded.schedule == "schedule"
    diagnostic = state.treasury_chain_read_diagnostics["epoch_schedule"]
    assert (diagnostic.attempts, diagnostic.successes, diagnostic.failures) == (2, 1, 1)
    assert diagnostic.last_failure_kind == "timeout"
    assert diagnostic.last_failure_request_id == "schedule-correlated"
    assert diagnostic.last_failure_elapsed_seconds >= 0
    assert "PRIVATE" not in diagnostic.model_dump_json()
    assert "PRIVATE" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage, cause, status",
    [
        ("identity", ChainTreasuryReadTimeoutError("uid_binding"), 503),
        ("setter_roster", ChainTreasuryReadTimeoutError("permit_vector"), 503),
        ("identity", ChainConnectionError("PRIVATE provider"), 503),
        ("identity", ConnectionResetError("PRIVATE reset"), 503),
        ("identity", ValueError("PRIVATE invalid evidence"), 428),
        ("identity", FileNotFoundError("PRIVATE missing file"), None),
    ],
)
async def test_requester_availability_does_not_bypass_authority(
    monkeypatch, stage, cause, status
):
    pin = EnforcingTreasuryPin.model_validate_json(
        (
            Path(__file__).resolve().parents[5]
            / "packages/ditto-screening-protocol/tests/fixtures"
            / "treasury_enforcing_pin_v2.json"
        ).read_text()
    )
    ledger = SimpleNamespace(treasury_pin=pin, stale=False, statistical_band_mode="off")
    state = SimpleNamespace()
    monkeypatch.setattr(
        scoring,
        "_gamma_runtime_or_503",
        AsyncMock(return_value=SimpleNamespace(treasury_weight_enforcement=True)),
    )
    error = ChainTreasuryActivationReadError(stage, cause)
    monkeypatch.setattr(
        treasury_weights, "require_enforcing_requester", AsyncMock(side_effect=error)
    )
    with pytest.raises(
        HTTPException if status else ChainTreasuryActivationReadError
    ) as rejected:
        await scoring._require_statistical_cap_requester(
            None, "synthetic", ledger, now=datetime.now(UTC), app_state=state
        )
    if status:
        assert rejected.value.status_code == status
        if status == 503:
            assert rejected.value.headers == {"Retry-After": "5"}
        assert "PRIVATE" not in rejected.value.detail
    assert not hasattr(state, "treasury_chain_read_diagnostics")


def _setup_producer(monkeypatch):
    from ditto.tests.api_server.test_treasury_weights import pin

    p = pin()
    runtime = SimpleNamespace(
        revision=1,
        treasury_weight_enforcement=True,
        activation_epoch=0,
        treasury_managed_validator_hotkeys=tuple(m.validator_hotkey for m in p.fleet),
        treasury_approved_policy_digest=p.policy_digest,
        treasury_approved_collector_policy_digest=p.policy.collector_policy_digest,
    )
    state = SimpleNamespace(
        chain=object(), config=SimpleNamespace(chain=SimpleNamespace(netuid=118))
    )
    monkeypatch.setattr(
        "ditto.api_server.treasury_runtime.treasury_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        treasury_weights, "read_treasury_fleet", AsyncMock(return_value=p.fleet)
    )
    return p, state


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["identity", "setter_roster"])
async def test_actual_chain_failure_records_phase_and_request_id(monkeypatch, stage):
    p, state = _setup_producer(monkeypatch)
    cause = ChainTreasuryReadTimeoutError("uid_binding")
    error = ChainTreasuryActivationReadError(stage, cause)
    monkeypatch.setattr(
        treasury_weights,
        "current_managed_dispatch_observation",
        AsyncMock(side_effect=error),
    )
    token = request_id_var.set("read-correlated")
    try:
        with pytest.raises(ChainTreasuryActivationReadError):
            await treasury_weights.require_enforcing_requester(
                None, p, p.fleet[0].validator_hotkey, app_state=state
            )
    finally:
        request_id_var.reset(token)
    diagnostic = state.treasury_chain_read_diagnostics["requester_activation"]
    assert diagnostic.failures == 1 and diagnostic.successes == 0
    assert diagnostic.last_failure_stage == stage
    assert diagnostic.last_failure_step == "uid_binding"
    assert diagnostic.last_failure_request_id == "read-correlated"
    assert "PRIVATE" not in diagnostic.model_dump_json()


@pytest.mark.asyncio
async def test_pre_read_readiness_rejection_does_not_count_as_chain_failure(
    monkeypatch,
):
    p, state = _setup_producer(monkeypatch)
    monkeypatch.setattr(
        treasury_weights, "read_treasury_fleet", AsyncMock(return_value=())
    )
    chain_read = AsyncMock()
    monkeypatch.setattr(
        treasury_weights, "current_managed_dispatch_observation", chain_read
    )
    with pytest.raises(ValueError, match="live managed fleet"):
        await treasury_weights.require_enforcing_requester(
            None, p, p.fleet[0].validator_hotkey, app_state=state
        )
    chain_read.assert_not_awaited()
    assert not hasattr(state, "treasury_chain_read_diagnostics")


@pytest.mark.asyncio
async def test_successful_chain_read_count_survives_post_read_authority_rejection(
    monkeypatch,
):
    p, state = _setup_producer(monkeypatch)
    observation = SimpleNamespace(
        identity=p.identity,
        epoch_index=p.epoch_index,
        first_block=p.first_block,
        finalized_block=p.identity.finalized_block,
        finalized_block_hash=p.identity.finalized_block_hash,
    )
    monkeypatch.setattr(
        treasury_weights,
        "current_managed_dispatch_observation",
        AsyncMock(return_value=observation),
    )

    def reject(*_args, **_kwargs):
        raise ValueError("synthetic authority rejection")

    monkeypatch.setattr(treasury_weights, "require_treasury_weight_authority", reject)
    with pytest.raises(ValueError, match="authority rejection"):
        await treasury_weights.require_enforcing_requester(
            None, p, p.fleet[0].validator_hotkey, app_state=state
        )
    diagnostic = state.treasury_chain_read_diagnostics["requester_activation"]
    assert (diagnostic.attempts, diagnostic.successes, diagnostic.failures) == (1, 1, 0)


def test_direct_call_without_app_state_still_logs_safely(monkeypatch):
    log = Mock()
    monkeypatch.setattr("ditto.api_server.treasury_read_diagnostics.logger.info", log)
    record_treasury_read(None, "epoch_schedule", elapsed=0.1)
    log.assert_called_once()
    assert log.call_args.args[1:3] == ("epoch_schedule", "success")
