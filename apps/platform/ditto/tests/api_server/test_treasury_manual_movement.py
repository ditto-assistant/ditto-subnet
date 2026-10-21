"""Real SQL + canonical chain decoder; missing allocation never becomes proof."""

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from ditto.api_models.treasury_ingress import TreasuryReceiptSelector
from ditto.api_server.treasury_aggregate import aggregate_receipt
from ditto.api_server.treasury_ingress import ReceiptHistoryUnavailable
from ditto.api_server.treasury_manual_movement import publish_finalized_movement
from ditto.db.models import (
    TreasuryManualTransfer,
    TreasuryPublicEvent,
    TreasuryVerifiedReceipt,
)
from ditto.tests.api_server.endpoints.test_treasury_receipt_ingress import (
    install,
    selection,
)
from ditto_screening_protocol.treasury_manual import ManualEnvelope, ManualReport

pytestmark = pytest.mark.asyncio


async def seed_movement(app, maker, **install_args):
    pin, settings, rpc = await install(app, maker, **install_args)
    coordinates = selection()
    envelope = ManualEnvelope(
        collector_policy_digest=pin.policy.collector_policy_digest,
        destination=settings.service_buckets[0].holding_coldkey,
        request={
            "request_id": str(uuid4()),
            "after_operation": 3,
            "source_block": 110,
            "bucket_id": "gamma",
            "amount_rao": 40,
            "retained_alpha_rao": 100,
            "expires_block": 300,
            "reason": "PRIVATE manual receipt reason",
        },
    )
    report = ManualReport(
        collector_policy_digest=envelope.collector_policy_digest,
        observed_at=int(time.time()),
        request_id=envelope.request.request_id,
        request_digest=envelope.digest,
        status="finalized",
        settlement={
            "epoch_index": 9,
            **{
                k: coordinates[k]
                for k in ("block", "block_hash", "extrinsic_index", "extrinsic_hash")
            },
        },
    )
    async with maker() as session, session.begin():
        session.add(
            TreasuryManualTransfer(
                request_id=envelope.request.request_id,
                envelope=envelope.model_dump(),
                digest=envelope.digest,
                actor="PRIVATE staff identity",
                status="audit_pending",
                report=report.model_dump(),
            )
        )
    return envelope.request.request_id, rpc


async def test_canonical_movement_publishes_without_claiming_allocation_or_sending(
    app,
    client,
    session_maker,
):
    request_id, rpc = await seed_movement(app, session_maker)
    async with session_maker() as session, session.begin():
        row = await session.get(TreasuryManualTransfer, request_id)
        await publish_finalized_movement(session, app.state.chain, row)
        assert row.status == "audit_pending"
        assert row.receipt["source_attribution"] == "incomplete"
        saved = row.receipt
        # Replay must not require another chain read, let alone send funds.
        await publish_finalized_movement(session, None, row)
        assert row.receipt == saved
        assert await session.scalar(select(TreasuryPublicEvent.id)) is None
        assert await session.scalar(select(TreasuryVerifiedReceipt.receipt_id)) is None
    response = await client.get("/api/v1/public/treasury-activity/manual-movements")
    assert response.status_code == 200
    public = response.json()
    assert len(public["items"]) == 1
    item = public["items"][0]
    assert item["deposit_amount_atomic"] == "40"
    assert item["extrinsic_hash"] == selection()["extrinsic_hash"]
    assert item["source_attribution"] == "incomplete"
    assert item["credited_usd_nano"] is None
    assert "PRIVATE" not in response.text
    assert "source_claims" not in item and "allocation_bps" not in item
    assert response.headers["Cache-Control"] == "public, max-age=5"


@pytest.mark.parametrize("invalid_payment", [False, True])
async def test_loop_backfills_prior_refusal_without_retrying_source_or_custody(
    app, client, session_maker, monkeypatch, invalid_payment
):
    from ditto.api_server import treasury_manual_loop as module

    request_id, rpc = await seed_movement(app, session_maker)
    refusal = "Finalized receipt proof refused; operator review required"
    async with session_maker() as session, session.begin():
        row = await session.get(TreasuryManualTransfer, request_id)
        row.last_error = refusal
    if invalid_payment:
        rpc.transfer_events[-1]["event"]["attributes"][3] = 41

    def forbidden(*_args, **_kwargs):
        raise AssertionError("refused source audit/custody must not be retried")

    monkeypatch.setattr(module, "publish_audit", forbidden)
    calls = []

    async def checked_movement(*args):
        calls.append(True)
        return await publish_finalized_movement(*args)

    monkeypatch.setattr(module, "publish_finalized_movement", checked_movement)
    loop = module.TreasuryManualLoop(
        app.state,
        mailbox=SimpleNamespace(pull=lambda: None, publish=forbidden, ack=forbidden),
    )
    await loop.sweep()
    await loop.sweep()
    async with session_maker() as session:
        row = await session.get(TreasuryManualTransfer, request_id)
        assert row.status == "audit_pending" and row.last_error == refusal
        assert bool(row.receipt) is not invalid_payment
        assert await session.scalar(select(TreasuryVerifiedReceipt.receipt_id)) is None
    public = (
        await client.get("/api/v1/public/treasury-activity/manual-movements")
    ).json()["items"]
    assert len(public) == (0 if invalid_payment else 1)
    # Success is retained once; unavailable/invalid proof never becomes a receipt.
    assert len(calls) == (2 if invalid_payment else 1)


@pytest.mark.parametrize(
    "fault", ["signature", "checksum", "ledger", "pin_identity", "future_policy"]
)
async def test_bad_historical_anchor_never_publishes(app, client, session_maker, fault):
    request_id, _ = await seed_movement(app, session_maker, fault=fault)
    with pytest.raises(ValueError):
        async with session_maker() as session, session.begin():
            row = await session.get(TreasuryManualTransfer, request_id)
            await publish_finalized_movement(session, app.state.chain, row)
    assert (
        await client.get("/api/v1/public/treasury-activity/manual-movements")
    ).json() == {"items": []}


@pytest.mark.parametrize(
    "fault", ["amount", "hash", "destination", "unfinalized", "report_digest"]
)
async def test_forged_or_changed_payment_never_publishes(
    app, client, session_maker, fault
):
    request_id, rpc = await seed_movement(app, session_maker)
    with pytest.raises(ValueError):
        async with session_maker() as session, session.begin():
            row = await session.get(TreasuryManualTransfer, request_id)
            if fault == "amount":
                rpc.transfer_events[-1]["event"]["attributes"][3] = 41
            elif fault == "unfinalized":
                rpc.height = 119
            elif fault == "hash":
                rpc.hashes[120] = "0x" + "f" * 64
            elif fault == "report_digest":
                row.report = {**row.report, "request_digest": "0" * 64}
            else:
                row.envelope = {**row.envelope, "destination": "5" + "a" * 47}
            await publish_finalized_movement(session, app.state.chain, row)
    assert (
        await client.get("/api/v1/public/treasury-activity/manual-movements")
    ).json() == {"items": []}


async def test_publication_off_remains_private(app, client, session_maker):
    request_id, _ = await seed_movement(app, session_maker, publish=False)
    with pytest.raises(ValueError, match="publication"):
        async with session_maker() as session, session.begin():
            row = await session.get(TreasuryManualTransfer, request_id)
            await publish_finalized_movement(session, app.state.chain, row)
    assert (
        await client.get("/api/v1/public/treasury-activity/manual-movements")
    ).json() == {"items": []}


async def test_missing_source_epoch_refuses_attribution_before_archive_reads(
    app, session_maker
):
    await install(app, session_maker)
    selector = TreasuryReceiptSelector(
        **{
            **selection(),
            "source_claims": [
                {"source_block": 110, "epoch_index": 9, "amount_rao": 20},
                {"source_block": 111, "epoch_index": 10, "amount_rao": 20},
            ],
        }
    )
    async with session_maker() as session:
        # No chain object exists: absence must be detected before archive work.
        with pytest.raises(ReceiptHistoryUnavailable, match="source epochs"):
            await aggregate_receipt(session, None, selector, record=True)


async def test_duplicate_chain_effect_cannot_publish_for_another_request(
    app,
    client,
    session_maker,
):
    request_id, _ = await seed_movement(app, session_maker)
    async with session_maker() as session, session.begin():
        row = await session.get(TreasuryManualTransfer, request_id)
        await publish_finalized_movement(session, app.state.chain, row)
        envelope = ManualEnvelope.model_validate(row.envelope)
        request = envelope.request.model_copy(update={"request_id": str(uuid4())})
        other = envelope.model_copy(update={"request": request})
        report = ManualReport.model_validate(row.report).model_copy(
            update={
                "request_id": request.request_id,
                "request_digest": other.digest,
            }
        )
        session.add(
            TreasuryManualTransfer(
                request_id=request.request_id,
                envelope=other.model_dump(),
                digest=other.digest,
                actor="PRIVATE second staff",
                status="audit_pending",
                report=report.model_dump(),
            )
        )
    with pytest.raises(ValueError, match="another receipt"):
        async with session_maker() as session, session.begin():
            row = await session.get(TreasuryManualTransfer, request.request_id)
            await publish_finalized_movement(session, app.state.chain, row)
    assert (
        len(
            (
                await client.get("/api/v1/public/treasury-activity/manual-movements")
            ).json()["items"]
        )
        == 1
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("public_sender", "altered sender"),
        ("event_at", "2030-01-01T00:00:00Z"),
        ("event_index", 99),
        ("payment_id", "altered receipt"),
    ],
)
async def test_cached_public_payload_integrity_is_checked_on_replay_and_read(
    app,
    session_maker,
    field,
    value,
):
    import copy

    from ditto.api_server.treasury_manual_movement import verified_movement_payload

    request_id, _ = await seed_movement(app, session_maker)
    async with session_maker() as session, session.begin():
        row = await session.get(TreasuryManualTransfer, request_id)
        await publish_finalized_movement(session, app.state.chain, row)
        changed = copy.deepcopy(row.receipt)
        changed["movement"][field] = value
        row.receipt = changed
        with pytest.raises(ValueError, match="proof changed"):
            await publish_finalized_movement(session, None, row)
        with pytest.raises(ValueError, match="proof changed"):
            verified_movement_payload(changed)


@pytest.mark.parametrize("cached", [[], {}, "malformed receipt", True])
async def test_malformed_cached_json_is_a_controlled_refusal(
    app, session_maker, cached
):
    request_id, _ = await seed_movement(app, session_maker)
    async with session_maker() as session:
        row = await session.get(TreasuryManualTransfer, request_id)
        row.receipt = cached
        with pytest.raises(ValueError, match="verified manual movement proof required"):
            await publish_finalized_movement(session, None, row)
