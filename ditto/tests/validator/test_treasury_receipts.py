"""V2 receipt authority cannot turn into a legacy transport fallback."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ditto.tests.validator.test_treasury_weights import fixture
from ditto.tests.validator.test_weight_receipts import finalized
from ditto.validator.weight_receipts import WeightReceiptRelay
from ditto_screening_protocol.weight_receipt import (
    FinalizedWeightReceipt,
    weight_receipt_signing_message,
    weight_vector_digest,
)


def context():
    pin, _ = fixture()
    ledger = SimpleNamespace(
        treasury_pin=pin,
        ledger_snapshot_id=uuid4(),
        epoch_index=pin.epoch_index,
        ledger_digest="a" * 64,
        active_bench_version=13,
    )
    weights = {pin.policy.collector_hotkey: 0.1, "burn": 0.9}
    return pin, ledger, weights


async def test_no_competitive_champion_v2_request_is_stable_and_binds_pin():
    pin, ledger, weights = context()
    calls = []

    async def accept(request_id, body):
        calls.append((request_id, body))
        return {
            "request_id": request_id,
            "request_digest": hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }

    relay = WeightReceiptRelay(
        SimpleNamespace(put_weights_with_receipt=accept),
        None,
        pin.fleet[0].validator_hotkey,
        118,
    )
    assert await relay.submit(weights, ledger, None) is True
    assert await relay.submit(weights, ledger, None) is True
    assert calls[0] == calls[1]
    body = calls[0][1]
    assert body["schema_version"] == 2
    assert body["treasury_pin"] == pin.model_dump(mode="json")
    assert body["provenance"]["champion_agent_id"] is None
    assert body["provenance"]["champion_artifact_sha256"] is None


@pytest.mark.parametrize(
    "fault",
    [
        "absent_transport",
        "unsupported",
        "timeout",
        "missing_snapshot",
        "wrong_epoch",
        "malformed_pin",
        "malformed_ack",
    ],
)
async def test_enforcing_relay_never_permits_legacy_fallback(fault):
    pin, ledger, weights = context()
    setter = SimpleNamespace(put_weights_with_receipt=AsyncMock(return_value=None))
    if fault == "absent_transport":
        setter = SimpleNamespace()
    elif fault == "timeout":
        setter.put_weights_with_receipt.side_effect = TimeoutError()
    elif fault == "missing_snapshot":
        ledger.ledger_snapshot_id = None
    elif fault == "wrong_epoch":
        ledger.epoch_index += 1
    elif fault == "malformed_pin":
        ledger.treasury_pin = {"version": 2}
    elif fault == "malformed_ack":
        setter.put_weights_with_receipt.return_value = {"request_id": "wrong"}
    relay = WeightReceiptRelay(setter, None, pin.fleet[0].validator_hotkey, 118)
    assert await relay.submit(weights, ledger, None) is False


def v2_claim():
    pin, ledger, weights = context()
    raw = finalized().model_dump(mode="json")
    raw.update(
        schema_version=2,
        treasury_pin=pin.model_dump(mode="json"),
        weights=weights,
        validator_hotkey=pin.fleet[0].validator_hotkey,
    )
    raw["provenance"].update(
        epoch_index=pin.epoch_index,
        champion_agent_id=None,
        champion_artifact_sha256=None,
        vector_digest=weight_vector_digest(weights),
    )
    raw["attempt"]["commit_block"] = 102
    body = {
        name: raw[name]
        for name in (
            "schema_version",
            "mechanism_id",
            "weights",
            "provenance",
            "treasury_pin",
        )
    }
    raw["request_digest"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return raw


def test_v2_claim_no_champion_and_versioned_signature_domain():
    claim = FinalizedWeightReceipt.model_validate(v2_claim())
    assert weight_receipt_signing_message(claim, 123).startswith(
        b"ditto-validator-weight-receipt:v2:"
    )
    legacy = finalized()
    assert "treasury_pin" not in legacy.model_dump(mode="json")
    assert weight_receipt_signing_message(legacy, 123).startswith(
        b"ditto-validator-weight-receipt:v1:"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_pin",
        "wrong_epoch",
        "wrong_validator",
        "before_pin",
        "unpaired_champion",
        "legacy_no_champion",
        "wrong_service",
        "bool_version",
    ],
)
def test_v2_shape_refuses_incomplete_or_rebound_authority(fault):
    raw = v2_claim()
    if fault == "missing_pin":
        raw.pop("treasury_pin")
    elif fault == "wrong_epoch":
        raw["provenance"]["epoch_index"] += 1
    elif fault == "wrong_validator":
        raw["validator_hotkey"] = "outsider"
    elif fault == "before_pin":
        raw["attempt"]["commit_block"] = 101
    elif fault == "unpaired_champion":
        raw["provenance"]["champion_agent_id"] = str(uuid4())
    elif fault == "legacy_no_champion":
        raw.update(schema_version=1, treasury_pin=None)
    elif fault == "wrong_service":
        raw["weights"] = {"burn": 1}
    else:
        raw["schema_version"] = True
    with pytest.raises(ValueError):
        FinalizedWeightReceipt.model_validate(raw)
