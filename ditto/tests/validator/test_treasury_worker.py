"""Actual worker fold and transport refusal with synthetic signed policies."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from bittensor_wallet import Keypair

from ditto.api_models.router_ledger import RouterLedgerResponse
from ditto.api_models.validator import LedgerResponse
from ditto.tests.validator.test_treasury_weights import fixture
from ditto.tests.validator.test_worker import _config, _entry
from ditto.validator.worker import ValidatorWorker
from ditto_screening_protocol.treasury_identity import TreasuryDispatchObservation


def make_worker(tmp_path, monkeypatch, *, empty=False, burn=0, service_bps=1000):
    pin, args = fixture(service_bps=service_bps)
    config = _config()
    config.validator_hotkey = pin.fleet[0].validator_hotkey
    config.netuid = 118
    config.burn_hotkey = args["burn_hotkey"]
    proof = tmp_path / "public-approval.json"
    proof.write_text(pin.approval.model_dump_json())
    config.treasury_approval_file = str(proof)
    config.treasury_approved_policy_digest = pin.policy_digest
    config.treasury_collector_policy_digest = pin.policy.collector_policy_digest
    entries = [] if empty else [_entry("miner", 0.8, bench_version=8)]
    # A would-be collector champion must be excluded before competition.
    entries.append(_entry(pin.policy.collector_hotkey, 1, bench_version=8))
    ledger = LedgerResponse(
        entries=entries,
        count=len(entries),
        treasury_pin=pin,
        ledger_snapshot_id=uuid4(),
        epoch_index=pin.epoch_index,
        pinned_block=pin.pinned_block,
        ledger_digest="a" * 64,
        burn_share=burn,
        active_bench_version=8,
    )
    observation = TreasuryDispatchObservation(
        identity=args["current_identity"],
        epoch_index=9,
        first_block=100,
        finalized_block=102,
        finalized_block_hash="0x" + "ac" * 32,
    )
    chain = MagicMock()
    chain.get_treasury_dispatch_observation = AsyncMock(return_value=observation)
    capability = pin.fleet[0].model_dump(
        exclude={"validator_hotkey", "protocol_version"}
    )
    chain.get_treasury_weight_capability = AsyncMock(return_value=capability)
    worker = ValidatorWorker(
        config=config,
        chain=chain,
        platform=SimpleNamespace(get_ledger=AsyncMock(return_value=ledger)),
        dittobench=MagicMock(),
        keypair=MagicMock(),
    )
    monkeypatch.setattr(
        worker, "_registered_ledger_entries", AsyncMock(side_effect=lambda rows: rows)
    )
    monkeypatch.setattr(
        worker, "_resolve_burn_hotkey", AsyncMock(return_value=config.burn_hotkey)
    )
    monkeypatch.setattr(
        worker, "_get_router_ledger", AsyncMock(return_value=RouterLedgerResponse())
    )
    monkeypatch.setattr(worker, "_validator_permitted", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_stake_sufficient", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_log_commit_reveal_mode", AsyncMock())
    monkeypatch.setattr(worker, "_put_weights_with_retry", AsyncMock(return_value=True))
    worker._weight_receipt_relay = SimpleNamespace(
        recover=AsyncMock(), submit=AsyncMock(return_value=True)
    )
    return worker, ledger, chain


@pytest.mark.parametrize(
    "fault",
    [None, "signature", "identity", "partial_config", "receipt", "untrusted_signer"],
)
async def test_external_worker_without_managed_config_verifies_pin_and_transport(
    tmp_path, monkeypatch, fault
):
    managed, _, _ = make_worker(tmp_path, monkeypatch)
    expected = (await managed._update_weights()).weights
    worker, ledger, chain = make_worker(tmp_path, monkeypatch)
    worker._config.validator_hotkey = Keypair.create_from_uri("//Dave").ss58_address
    worker._config.treasury_approval_file = None
    worker._config.treasury_approved_policy_digest = None
    worker._config.treasury_collector_policy_digest = None
    chain.get_treasury_weight_capability.return_value = None
    if fault != "untrusted_signer":
        policy = ledger.treasury_pin.policy
        monkeypatch.setattr(
            "ditto_screening_protocol.treasury_approval.SN118_FOLLOWER_AUTHORITY",
            (policy.genesis_hash, policy.netuid, policy.collector_coldkey),
        )
    if fault == "signature":
        pin = ledger.treasury_pin
        worker._platform.get_ledger.return_value = ledger.model_copy(
            update={
                "treasury_pin": pin.model_copy(
                    update={
                        "approval": pin.approval.model_copy(
                            update={"signature": "0x" + "00" * 64}
                        )
                    }
                )
            }
        )
    elif fault == "identity":
        observed = chain.get_treasury_dispatch_observation.return_value
        chain.get_treasury_dispatch_observation.return_value = observed.model_copy(
            update={"identity": observed.identity.model_copy(update={"uid": 1})}
        )
    elif fault == "partial_config":
        worker._config.treasury_approved_policy_digest = "a" * 64
    elif fault == "receipt":
        worker._weight_receipt_relay.submit.return_value = None
    result = await worker._update_weights()
    assert result.submitted is (fault is None)
    if fault is None:
        assert result.weights == expected
        chain.get_treasury_dispatch_observation.assert_awaited_once_with(
            ledger.treasury_pin.policy
        )
        worker._weight_receipt_relay.submit.assert_awaited_once()
    elif fault != "receipt":
        worker._weight_receipt_relay.submit.assert_not_awaited()
    worker._put_weights_with_retry.assert_not_awaited()
    chain.get_treasury_weight_capability.assert_not_awaited()


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("burn", [0, 0.5, 1])
@pytest.mark.parametrize("service_bps", [0, 1000, 2500, 7500, 10000])
async def test_worker_service_precedes_burn_and_collector_never_competes(
    tmp_path,
    monkeypatch,
    empty,
    burn,
    service_bps,
):
    worker, ledger, _ = make_worker(
        tmp_path, monkeypatch, empty=empty, burn=burn, service_bps=service_bps
    )
    result = await worker._update_weights()
    assert result.submitted
    collector = ledger.treasury_pin.policy.collector_hotkey
    assert result.weights.get(collector, 0) == service_bps / 10_000
    miner = 0 if empty else (1 - service_bps / 10_000) * (1 - burn)
    assert result.weights.get("miner", 0) == pytest.approx(miner)
    assert sum(result.weights.values()) == pytest.approx(1)
    worker._put_weights_with_retry.assert_not_awaited()
    submitted_champion = worker._weight_receipt_relay.submit.await_args.args[2]
    if submitted_champion is not None:
        assert submitted_champion.miner_hotkey != collector


@pytest.mark.parametrize(
    "fault",
    [
        "missing_proof",
        "missing_guard",
        "wrong_policy",
        "uid_drift",
        "stale",
        "unknown_pin",
        "wrong_epoch",
        "receipt_none",
        "receipt_false",
    ],
)
async def test_worker_refuses_enforcing_faults_without_legacy_fallback(
    tmp_path, monkeypatch, fault
):
    worker, ledger, chain = make_worker(tmp_path, monkeypatch)
    if fault == "missing_proof":
        worker._config.treasury_approval_file = None
    elif fault == "missing_guard":
        chain.get_treasury_weight_capability.return_value = None
    elif fault == "wrong_policy":
        chain.get_treasury_weight_capability.return_value["approved_policy_digest"] = (
            "f" * 64
        )
    elif fault == "uid_drift":
        observation = chain.get_treasury_dispatch_observation.return_value
        chain.get_treasury_dispatch_observation.return_value = observation.model_copy(
            update={"identity": observation.identity.model_copy(update={"uid": 1})}
        )
    elif fault == "stale":
        worker._platform.get_ledger.return_value = ledger.model_copy(
            update={"stale": True}
        )
    elif fault == "unknown_pin":
        worker._platform.get_ledger.return_value = ledger.model_copy(
            update={"treasury_pin": {"version": 3}}
        )
    elif fault == "wrong_epoch":
        worker._platform.get_ledger.return_value = ledger.model_copy(
            update={"epoch_index": 10}
        )
    else:
        worker._weight_receipt_relay.submit.return_value = (
            None if fault == "receipt_none" else False
        )
    result = await worker._update_weights()
    assert not result.submitted
    worker._put_weights_with_retry.assert_not_awaited()
    if fault not in {"receipt_none", "receipt_false"}:
        worker._weight_receipt_relay.submit.assert_not_awaited()
