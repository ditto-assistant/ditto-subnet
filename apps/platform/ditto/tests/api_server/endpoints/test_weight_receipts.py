"""Authenticated immutable commit claims, including same-hotkey artifact reuse."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import bittensor
import pytest
from sqlalchemy import func, select

from ditto.api_models.weight_receipt import (
    FinalizedWeightReceipt,
    weight_receipt_signing_message,
    weight_vector_digest,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.ledger_pin import ledger_digest
from ditto.chain.models import NeuronInfo
from ditto.db.models import (
    LedgerEpochSnapshot,
    ValidatorWeightReceipt,
    ValidatorWeightRequest,
)

pytestmark = pytest.mark.asyncio
_KEY = bittensor.Keypair.create_from_uri("//Alice")
_HOTKEY = _KEY.ss58_address


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


async def _setup(app, maker):
    async def db():
        async with maker() as session:
            yield session

    async def chain():
        result = MagicMock()
        result.get_recent_neurons = AsyncMock(
            return_value=[
                NeuronInfo(
                    hotkey=_HOTKEY,
                    coldkey="cold",
                    uid=1,
                    stake=1000.0,
                    validator_permit=True,
                )
            ]
        )
        return result

    app.dependency_overrides[get_session] = db
    app.dependency_overrides[get_chain_client] = chain
    agent_id, snapshot_id = uuid4(), uuid4()
    entries = [
        {
            "agent_id": str(agent_id),
            "sha256": "ab" * 32,
            "miner_hotkey": "miner",
            "composite": 0.9,
            "n": 120,
            "first_seen": datetime.now(UTC).isoformat(),
            "run_id": "run",
            "seed": 1,
            "validator_hotkey": _HOTKEY,
            "status": "scored",
            "bench_version": 13,
        }
    ]
    pin_digest = ledger_digest(entries, {"burn_share": 0.1})
    async with maker() as session, session.begin():
        session.add(
            LedgerEpochSnapshot(
                snapshot_id=snapshot_id,
                netuid=app.state.config.chain.netuid,
                epoch_index=123,
                last_epoch_block=100,
                pinned_block=101,
                pinned_block_hash="0x" + "01" * 32,
                pinned_at=datetime.now(UTC),
                bench_version=13,
                entries=entries,
                context={"served": {"burn_share": 0.1}},
                ledger_digest=pin_digest,
                champion_agent_id=agent_id,
            )
        )
    weights = {"miner": 0.9, "owner": 0.1}
    provenance = {
        "ledger_snapshot_id": str(snapshot_id),
        "epoch_index": 123,
        "ledger_digest": pin_digest,
        "champion_agent_id": str(agent_id),
        "champion_artifact_sha256": "ab" * 32,
        "bench_version": 13,
        "vector_digest": weight_vector_digest(weights),
    }
    request = {
        "schema_version": 1,
        "mechanism_id": 0,
        "weights": weights,
        "provenance": provenance,
    }
    return dict(
        **request,
        request_id=str(uuid4()),
        task_id=1,
        request_digest=_digest(request),
        netuid=app.state.config.chain.netuid,
        validator_hotkey=_HOTKEY,
        attempt={
            "attempt_id": str(uuid4()),
            "normalized_weights": [[0, 6553], [1, 58982]],
            "ciphertext_hash": hashlib.blake2b(b"cipher", digest_size=32).hexdigest(),
            "ciphertext_hex": b"cipher".hex(),
            "reveal_round": 1234,
            "version_key": 1,
            "commit_block": 110,
            "commit_block_hash": "0x" + "02" * 32,
            "extrinsic_hash": "0x" + "03" * 32,
            "extrinsic_index": 2,
        },
    )


def _signed(raw, *, timestamp=None):
    model = FinalizedWeightReceipt.model_validate(raw)
    timestamp = int(datetime.now(UTC).timestamp()) if timestamp is None else timestamp
    return {
        "receipt": raw,
        "timestamp": timestamp,
        "signature": _KEY.sign(weight_receipt_signing_message(model, timestamp)).hex(),
    }


async def _post(client, body):
    return await client.post(
        "/api/v1/validator/weight-submission-receipt",
        headers={"X-Validator-Hotkey": _HOTKEY},
        json=body,
    )


async def test_signed_receipt_retry_is_idempotent_and_unknown_fields_ignored(
    app, client, session_maker
):
    raw = await _setup(app, session_maker)
    first = await _post(client, _signed(raw))
    assert first.status_code == 200, first.text
    raw["future_field"] = "not authoritative"
    raw["attempt"]["future_field"] = "ignored"
    again = await _post(client, _signed(raw))
    assert again.status_code == 200, again.text
    assert first.json() == again.json()
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ValidatorWeightReceipt)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(ValidatorWeightRequest)
            )
            == 1
        )
        row = await session.scalar(select(ValidatorWeightReceipt))
        assert row is not None
        assert "future_field" not in row.receipt


async def test_unsigned_tampering_and_stale_signature_rejected(
    app, client, session_maker
):
    raw = await _setup(app, session_maker)
    body = _signed(raw)
    body["receipt"]["attempt"]["commit_block"] += 1
    response = await _post(client, body)
    assert response.status_code == 401, response.text
    response = await _post(client, _signed(raw, timestamp=1))
    assert response.status_code == 401, response.text


async def test_same_job_or_attempt_cannot_be_rebound(app, client, session_maker):
    raw = await _setup(app, session_maker)
    first = await _post(client, _signed(raw))
    assert first.status_code == 200, first.text
    raw["attempt"]["commit_block"] += 1
    assert (await _post(client, _signed(raw))).status_code == 409
    raw["attempt"]["attempt_id"] = str(uuid4())
    raw["task_id"] += 1
    assert (await _post(client, _signed(raw))).status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("champion_agent_id", "11111111-2222-4333-8444-555555555555"),
        ("champion_artifact_sha256", "ff" * 32),
        ("epoch_index", 124),
        ("ledger_digest", "ef" * 32),
        ("bench_version", 14),
    ],
)
async def test_same_hotkey_and_vector_cannot_name_another_artifact_or_pin(
    app, client, session_maker, field, value
):
    raw = await _setup(app, session_maker)
    raw["provenance"][field] = value
    raw["request_digest"] = _digest(
        {
            key: raw[key]
            for key in ("schema_version", "mechanism_id", "weights", "provenance")
        }
    )
    response = await _post(client, _signed(raw))
    assert response.status_code == 409, response.text
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ValidatorWeightReceipt)
            )
            == 0
        )
