import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from ditto.api_server.endpoints import admin_validator_weights as endpoint
from ditto.api_server.ledger_pin import (
    LedgerPin,
    canonical_entries,
    ledger_digest,
    pin_expected_shares,
)
from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainWeight, ChainWeightsSnapshot, ChainWeightVector
from ditto.chain.weight_diagnostics import PendingWeightCommit, WeightDiagnostics
from ditto.db.models import LedgerEpochSnapshot, ValidatorHeartbeat
from ditto.tests.api_server.endpoints.test_public import _seed_pin

URL = "/api/v1/admin/validator-weight-diagnostics"
TOKEN = "test-admin-token-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
HOTKEY = "5" + "A" * 47


@pytest.fixture(autouse=True)
def installed_chain(app):
    # ASGI unit clients do not run the application's lifespan.
    app.state.chain = object()


def fixture():
    trust = [0] * 140
    trust[139] = 60947
    updates = [0] * 140
    updates[139] = 9_029_448
    return WeightDiagnostics(
        ChainWeightsSnapshot(
            118,
            9_029_509,
            "0x" + "ab" * 32,
            None,
            (ChainWeightVector(139, HOTKEY, (ChainWeight(43, HOTKEY, 65535),)),),
        ),
        tuple(trust),
        tuple(updates),
        (65535,),
        9_029_509,
        0,
        25017,
        (
            # UID 139's legacy commit: round 32049696 is 9029577, inside its
            # own epoch 25016, whose boundary was 9029509.
            PendingWeightCommit(HOTKEY, 25016, 9_029_448, 32049696, 1_788_950_770),
            PendingWeightCommit(HOTKEY, 25017, 9_029_509, 32050000),
        ),
        360,
        0,
        9_029_869,
    )


async def test_admin_read_binds_trust_and_pending_to_one_block(
    app, client, monkeypatch
):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    reader = AsyncMock(return_value=fixture())
    monkeypatch.setattr(endpoint, "read_weight_diagnostics", reader)
    response = await client.get(URL + "?validator_uid=139", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["block"] == 9_029_509
    assert body["validators"][0]["validator_trust"] == pytest.approx(60947 / 65535)
    assert (body["tempo"], body["next_epoch_block"]) == (360, 9_029_869)
    legacy, unknown = body["pending_commits"]
    assert legacy["reveal_round"] == 32049696
    assert legacy["implied_reveal_block"] == 9_029_448 + round(
        (1_692_803_367 + 32049696 * 3 - 1_788_950_770) / 12
    )
    assert legacy["implied_reveal_offset_blocks"] == (
        legacy["implied_reveal_block"] - 9_029_509
    )
    assert unknown["commit_block_timestamp"] is None
    assert unknown["implied_reveal_block"] is None
    assert unknown["implied_reveal_offset_blocks"] is None
    assert body["weights_submitted"] is body["historical_clipping_verified"] is False
    assert "ciphertext" not in response.text


async def test_auth_and_bad_uid_do_not_read_chain(app, client, monkeypatch):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    reader = AsyncMock(return_value=fixture())
    monkeypatch.setattr(endpoint, "read_weight_diagnostics", reader)
    assert (await client.get(URL)).status_code == 401
    assert (
        await client.get(URL + "?validator_uid=-1", headers=HEADERS)
    ).status_code == 422
    reader.assert_not_awaited()


async def test_failed_or_missing_evidence_never_looks_healthy(app, client, monkeypatch):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    reader = AsyncMock(side_effect=ChainConnectionError("down"))
    monkeypatch.setattr(endpoint, "read_weight_diagnostics", reader)
    assert (await client.get(URL, headers=HEADERS)).status_code == 503
    reader.side_effect = None
    reader.return_value = fixture()
    assert (
        await client.get(URL + "?validator_uid=138", headers=HEADERS)
    ).status_code == 404
    reader.return_value = replace(fixture(), validator_trust=())
    assert (await client.get(URL, headers=HEADERS)).status_code == 503


@pytest.mark.parametrize(
    "verdict,snapshot_epoch,vector_variant,future_pin,invalid_epoch",
    [
        ("current", 25017, "current", False, None),
        ("previous", 25017, "previous", False, None),
        ("previous", 25017, "previous", True, None),
        ("diverged", 25017, "diverged", False, None),
        ("current", 25016, "previous", False, None),
        ("diverged", 25016, "current", False, None),
        ("unknown", 25018, "current", False, None),
        ("current", 25017, "current", False, 25016),
        ("unknown", 25017, "previous", False, 25016),
        ("unknown", 25017, "diverged", False, 25016),
        ("unknown", 25017, "previous", False, 25017),
    ],
)
async def test_pin_and_fold_provenance_preserves_fresh_chain_evidence(
    app,
    client,
    session_maker,
    monkeypatch,
    verdict,
    snapshot_epoch,
    vector_variant,
    future_pin,
    invalid_epoch,
    caplog,
):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    app.state.session_maker = session_maker
    a, b = uuid4(), uuid4()
    miner_a, miner_b, burn = "5" + "B" * 47, "5" + "C" * 47, "5" + "D" * 47
    await _seed_pin(
        session_maker, epoch_index=25016, champion=(b, miner_b), tail=(a, miner_a)
    )
    await _seed_pin(
        session_maker, epoch_index=25017, champion=(a, miner_a), tail=(b, miner_b)
    )
    if future_pin:
        await _seed_pin(
            session_maker, epoch_index=25018, champion=(a, miner_a), tail=(b, miner_b)
        )
    now = datetime.now(UTC)
    fold = {
        "epoch_index": 25017,
        "ledger_digest": "ab" * 32,
        "vector_digest": "cd" * 32,
        "folded_at": int(now.timestamp()),
    }
    async with session_maker() as session, session.begin():
        if invalid_epoch is not None:
            await session.execute(
                update(LedgerEpochSnapshot)
                .where(LedgerEpochSnapshot.epoch_index == invalid_epoch)
                .values(entries=[{"agent_id": "private-invalid-id"}])
            )
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=HOTKEY,
                software_version="0.356.9",
                protocol_version=30,
                code_digest="ab" * 32,
                state="idle",
                reported_at=now,
                seen_at=now,
                signature="ef" * 64,
                weights_fold=fold,
            )
        )
    values = {
        "current": (42598, 9175),
        "previous": (9175, 42598),
        "diverged": (40000, 10000),
    }[vector_variant]
    evidence = fixture()
    snapshot = replace(
        evidence.snapshot,
        owner_hotkey=burn,
        vectors=(
            ChainWeightVector(
                139,
                HOTKEY,
                (
                    ChainWeight(1, miner_a, values[0]),
                    ChainWeight(2, miner_b, values[1]),
                    ChainWeight(3, burn, 13762),
                ),
            ),
        ),
    )
    reader = AsyncMock(
        return_value=replace(
            evidence, snapshot=snapshot, subnet_epoch_index=snapshot_epoch
        )
    )
    monkeypatch.setattr(endpoint, "read_weight_diagnostics", reader)
    body = (await client.get(URL + "?validator_uid=139", headers=HEADERS)).json()
    observation = body["validators"][0]
    assert observation["matches_pin"] == verdict
    assert observation["fold"] == {**fold, "champion_agent_id": None}
    assert observation["validator_trust_u16"] == 60947
    assert observation["last_update_block"] == 9_029_448
    assert observation["weights"][-1]["hotkey"] == burn
    assert body["block"] == snapshot.block
    assert body["subnet_epoch_index"] == snapshot_epoch
    assert body["weights_submitted"] is body["historical_clipping_verified"] is False
    reader.assert_awaited_once()
    if invalid_epoch is not None:
        assert f"kind=invalid_stored_pin epoch={invalid_epoch}" in caplog.text
        assert "private-invalid-id" not in caplog.text


@pytest.mark.parametrize(
    "invalid_pin",
    [
        {"entries": [{"agent_id": "private-invalid-id"}]},
        {"context": {"served": {"treasury_pin": {"version": 2, "mode": "enforce"}}}},
    ],
)
async def test_invalid_stored_pin_keeps_fresh_chain_evidence(
    app, client, session_maker, monkeypatch, caplog, invalid_pin
):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    app.state.session_maker = session_maker
    await _seed_pin(
        session_maker,
        epoch_index=25017,
        champion=(uuid4(), "5" + "B" * 47),
        tail=(uuid4(), "5" + "C" * 47),
    )
    async with session_maker() as session, session.begin():
        await session.execute(
            update(LedgerEpochSnapshot)
            .where(LedgerEpochSnapshot.epoch_index == 25017)
            .values(**invalid_pin)
        )
    monkeypatch.setattr(
        endpoint, "read_weight_diagnostics", AsyncMock(return_value=fixture())
    )
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200, response.text
    observation = response.json()["validators"][0]
    assert observation["matches_pin"] == "unknown"
    assert observation["last_update_block"] == 9_029_448
    assert observation["validator_trust_u16"] == 60947
    assert "private-invalid-id" not in response.text
    assert "kind=invalid_stored_pin" in caplog.text
    assert "private-invalid-id" not in caplog.text


async def test_missing_stored_burn_share_keeps_fresh_chain_evidence(
    app, client, session_maker, monkeypatch, caplog
):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    app.state.session_maker = session_maker
    await _seed_pin(
        session_maker,
        epoch_index=25017,
        champion=(uuid4(), "5" + "B" * 47),
        tail=(uuid4(), "5" + "C" * 47),
    )
    fixture_path = (
        Path(__file__).resolve().parents[6]
        / "packages/ditto-screening-protocol/tests/fixtures"
        / "treasury_enforcing_pin_v2.json"
    )
    treasury = json.loads(fixture_path.read_text())
    async with session_maker() as session, session.begin():
        pin = (
            await session.scalars(
                select(LedgerEpochSnapshot).where(
                    LedgerEpochSnapshot.epoch_index == 25017
                )
            )
        ).one()
        treasury.update(
            epoch_index=pin.epoch_index,
            first_block=pin.last_epoch_block,
            pinned_block=pin.pinned_block,
        )
        treasury["identity"]["finalized_block"] = pin.pinned_block
        served = {"crown_mode": None, "treasury_pin": treasury}
        pin.context = {"served": served}
        pin.ledger_digest = ledger_digest(
            canonical_entries(LedgerPin.from_row(pin).entries), served
        )
        # Exercise the real projection's missing-key path, not a mock exception.
        with pytest.raises(KeyError, match="burn_share"):
            pin_expected_shares(pin)
    monkeypatch.setattr(
        endpoint, "read_weight_diagnostics", AsyncMock(return_value=fixture())
    )
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200, response.text
    observation = response.json()["validators"][0]
    assert observation["matches_pin"] == "unknown"
    assert observation["last_update_block"] == 9_029_448
    assert observation["validator_trust_u16"] == 60947
    assert "kind=invalid_stored_pin epoch=25017" in caplog.text
    assert treasury["approval"]["signature"] not in caplog.text


async def test_provenance_database_failure_keeps_fresh_chain_read(
    app, client, monkeypatch
):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)

    # The shared decoration reader fails softly even when a DB is configured.
    def unavailable_session():
        raise OperationalError("read", {}, None)

    app.state.session_maker = unavailable_session
    monkeypatch.setattr(
        endpoint, "read_weight_diagnostics", AsyncMock(return_value=fixture())
    )
    response = await client.get(URL, headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["validators"][0]["matches_pin"] == "unknown"
    assert body["validators"][0]["fold"] is None
    assert body["validators"][0]["last_update_block"] == 9_029_448
