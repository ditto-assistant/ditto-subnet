"""Receipt liveness/idempotency and restart-safe forwarding regressions."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from ditto.api_models.weight_receipt import (
    FinalizedWeightReceipt,
    SubmitWeightReceiptResponse,
    weight_receipt_digest,
    weight_vector_digest,
)
from ditto.validator.platform import PlatformClient
from ditto.validator.weight_receipts import WeightReceiptRelay


def context():
    return SimpleNamespace(
        ledger_snapshot_id=uuid4(),
        epoch_index=12,
        ledger_digest="a" * 64,
        active_bench_version=13,
    ), SimpleNamespace(agent_id=uuid4(), sha256="b" * 64, bench_version=13)


def finalized():
    ledger, champion = context()
    weights = {"miner": 0.9, "burn": 0.1}
    body = {
        "schema_version": 1,
        "mechanism_id": 0,
        "weights": weights,
        "provenance": {
            "ledger_snapshot_id": str(ledger.ledger_snapshot_id),
            "epoch_index": ledger.epoch_index,
            "ledger_digest": ledger.ledger_digest,
            "champion_agent_id": str(champion.agent_id),
            "champion_artifact_sha256": champion.sha256,
            "bench_version": champion.bench_version,
            "vector_digest": weight_vector_digest(weights),
        },
    }
    attempt = {
        "attempt_id": str(uuid4()),
        "normalized_weights": [[0, 7282], [1, 65535]],
        "ciphertext_hex": b"ciphertext".hex(),
        "ciphertext_hash": hashlib.blake2b(b"ciphertext", digest_size=32).hexdigest(),
        "reveal_round": 100,
        "version_key": 1,
        "commit_block": 100,
        "commit_block_hash": "0x" + "c" * 64,
        "extrinsic_hash": "0x" + "d" * 64,
        "extrinsic_index": 2,
    }
    return FinalizedWeightReceipt.model_validate(
        {
            **body,
            "request_id": str(uuid4()),
            "request_digest": hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "task_id": 1,
            "validator_hotkey": "validator",
            "netuid": 118,
            "attempt": attempt,
        }
    )


def envelope(claim):
    data = claim.model_dump(mode="json")
    attempt = data.pop("attempt")
    return {
        **data,
        "acknowledged": False,
        "status": "finalized",
        "attempts": [{**attempt, "status": "finalized"}],
    }


async def test_restart_retry_uses_same_id_but_identical_vector_new_artifact_does_not():
    calls = []

    async def accept(request_id, body):
        calls.append((request_id, body))
        return {
            "request_id": request_id,
            "request_digest": hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }

    setter = SimpleNamespace(put_weights_with_receipt=accept)
    ledger, champion = context()
    first = WeightReceiptRelay(setter, None, "validator", 118)
    second = WeightReceiptRelay(setter, None, "validator", 118)
    assert await first.submit({"miner": 1.0}, ledger, champion) is True
    assert await second.submit({"miner": 1.0}, ledger, champion) is True
    assert calls[0][0] == calls[1][0]
    champion.agent_id = uuid4()
    assert await second.submit({"miner": 1.0}, ledger, champion) is True
    assert calls[1][0] != calls[2][0]


async def test_timeout_is_uncertain_never_legacy_fallback():
    ledger, champion = context()
    setter = SimpleNamespace(
        put_weights_with_receipt=AsyncMock(side_effect=TimeoutError())
    )
    relay = WeightReceiptRelay(setter, None, "validator", 118)
    assert await relay.submit({"miner": 1.0}, ledger, champion) is False
    setter.put_weights_with_receipt = AsyncMock(return_value=None)
    assert await relay.submit({"miner": 1.0}, ledger, champion) is None
    assert await relay.submit({"miner": 1.0}, SimpleNamespace(), champion) is None


async def test_restart_recovers_exact_ack_before_pylon_ack():
    claim = finalized()
    setter = SimpleNamespace(
        list_weight_receipts=AsyncMock(
            return_value={"receipts": [envelope(claim)], "next_after_task_id": None}
        ),
        acknowledge_weight_receipt=AsyncMock(),
    )
    platform = SimpleNamespace(
        submit_weight_receipt=AsyncMock(
            return_value=SubmitWeightReceiptResponse(
                request_id=claim.request_id,
                attempt_id=claim.attempt.attempt_id,
                receipt_digest=weight_receipt_digest(claim),
            )
        )
    )
    relay = WeightReceiptRelay(setter, platform, "validator", 118)
    await relay.recover()
    platform.submit_weight_receipt.assert_awaited_once_with(claim)
    setter.acknowledge_weight_receipt.assert_awaited_once()
    assert setter.acknowledge_weight_receipt.call_args.args[1][
        "receipt_digest"
    ] == weight_receipt_digest(claim)


@pytest.mark.parametrize(
    "failure", ["wrong_ack", "platform_timeout", "wrong_identity", "prepared"]
)
async def test_invalid_or_unpersisted_receipt_stays_unacknowledged(failure):
    claim = finalized()
    data = envelope(claim)
    response = SubmitWeightReceiptResponse(
        request_id=claim.request_id,
        attempt_id=claim.attempt.attempt_id,
        receipt_digest="f" * 64,
    )
    if failure == "wrong_identity":
        data["validator_hotkey"] = "another-validator"
    if failure == "prepared":
        data["attempts"][0]["status"] = "prepared"
    setter = SimpleNamespace(
        list_weight_receipts=AsyncMock(
            return_value={"receipts": [data], "next_after_task_id": None}
        ),
        acknowledge_weight_receipt=AsyncMock(),
    )
    platform = SimpleNamespace(
        submit_weight_receipt=AsyncMock(
            return_value=response,
            side_effect=TimeoutError() if failure == "platform_timeout" else None,
        )
    )
    await WeightReceiptRelay(setter, platform, "validator", 118).recover()
    setter.acknowledge_weight_receipt.assert_not_awaited()


async def test_platform_post_signs_immutable_receipt_and_checks_ack():
    claim = finalized()
    signed = []
    signer = SimpleNamespace(sign=lambda message: signed.append(message) or b"x" * 64)

    def handler(request):
        assert request.url.path == "/api/v1/validator/weight-submission-receipt"
        assert request.headers["X-Validator-Hotkey"] == "validator"
        body = json.loads(request.content)
        assert body["receipt"] == claim.model_dump(mode="json")
        return httpx.Response(
            200,
            json={
                "stored": True,
                "request_id": str(claim.request_id),
                "attempt_id": str(claim.attempt.attempt_id),
                "receipt_digest": weight_receipt_digest(claim),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        platform = PlatformClient(
            SimpleNamespace(
                platform_api_url="https://platform.invalid",
                validator_hotkey="validator",
            ),
            http,
            signer,
        )
        result = await platform.submit_weight_receipt(claim)
    assert result.stored is True
    assert signed[0].startswith(b"ditto-validator-weight-receipt:v1:")


async def test_pin_benchmark_identity_wins_over_compatible_champion_version():
    ledger, champion = context()
    champion.bench_version = 12
    accepted = []

    async def submit(request_id, body):
        accepted.append(body)
        return {
            "request_id": request_id,
            "request_digest": hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }

    relay = WeightReceiptRelay(
        SimpleNamespace(put_weights_with_receipt=submit), None, "validator", 118
    )
    assert await relay.submit({"miner": 1.0}, ledger, champion) is True
    assert accepted[0]["provenance"]["bench_version"] == 13
    champion.sha256 = "malformed"
    assert await relay.submit({"miner": 1.0}, ledger, champion) is None
    assert len(accepted) == 1
    assert await relay.submit({"miner": 1.0}, ledger, object()) is None


async def test_one_malformed_receipt_does_not_starve_later_valid_receipt():
    claim = finalized()
    setter = SimpleNamespace(
        list_weight_receipts=AsyncMock(
            return_value={
                "receipts": [{"attempts": [{"status": "finalized"}]}, envelope(claim)],
                "next_after_task_id": 20,
            }
        ),
        acknowledge_weight_receipt=AsyncMock(),
    )
    platform = SimpleNamespace(
        submit_weight_receipt=AsyncMock(
            return_value=SubmitWeightReceiptResponse(
                request_id=claim.request_id,
                attempt_id=claim.attempt.attempt_id,
                receipt_digest=weight_receipt_digest(claim),
            )
        )
    )
    relay = WeightReceiptRelay(setter, platform, "validator", 118)
    await relay.recover()
    setter.acknowledge_weight_receipt.assert_awaited_once()
    assert relay.cursor == 20


async def test_high_frequency_heartbeats_schedule_at_most_one_recovery_per_30s(
    monkeypatch,
):
    import asyncio

    import ditto.validator.weight_receipts as module

    now = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    relay = WeightReceiptRelay(None, None, "validator", 118)
    relay.recover = AsyncMock()
    for _ in range(100):
        relay.schedule_recovery()
        await asyncio.sleep(0)
    relay.recover.assert_awaited_once()
    now[0] = 29.9
    relay.schedule_recovery()
    await asyncio.sleep(0)
    relay.recover.assert_awaited_once()
    now[0] = 30.0
    relay.schedule_recovery()
    await asyncio.sleep(0)
    assert relay.recover.await_count == 2
