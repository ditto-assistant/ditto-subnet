"""Treasury feed must publish exact receipts without private GM or signer data."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from ditto.db.models import TreasuryPublicEvent


@pytest.mark.asyncio
async def test_empty_then_exact_public_receipt_and_cursor(app, client, session_maker):
    app.state.session_maker = session_maker
    assert (await client.get("/api/v1/public/treasury-activity")).json() == {
        "items": [],
        "next_before": None,
    }
    async with session_maker() as session:
        for payment_id in ("payment-1", "payment-2"):
            session.add(
                TreasuryPublicEvent(
                    payment_id=payment_id,
                    event_kind="gm_token_deposit",
                    state="chain_finalized",
                    finalized_event_id=None,
                    event_at=datetime(2026, 9, 25, tzinfo=UTC),
                    policy_revision=3,
                    burn_revision=8,
                    burn_share_micros=0,
                    denominator="released_miner_emission",
                    maintenance_bps=25,
                    gm_bps=25,
                    allocation_bps=25,
                    allocated_alpha_rao=9_007_199_254_740_993,
                    source_alpha_rao=500,
                    route="alpha_to_tao",
                    deposit_asset="TAO",
                    deposit_amount_atomic=9_007_199_254_740_993,
                    credited_usd_nano=None,
                    bounty_award_id=None,
                    accepted_work_ref=None,
                    public_sender="public-sender",
                    public_recipient="public-recipient",
                    block_hash=f"0x{payment_id}",
                    extrinsic_index=2,
                    event_index=3,
                    actor_provenance="treasury_signer",
                    actor_public_id="signer-1",
                    verification_source="finalized_chain_rpc",
                )
            )
        await session.commit()
    response = await client.get("/api/v1/public/treasury-activity?limit=1")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=5"
    page = response.json()
    assert page["items"][0]["payment_id"] == "payment-2"
    assert page["items"][0]["block_hash"] == "0xpayment-2"
    assert page["items"][0]["allocation_bps"] == 25
    assert page["items"][0]["allocated_alpha_rao"] == "9007199254740993"
    assert page["items"][0]["deposit_amount_atomic"] == "9007199254740993"
    assert page["items"][0]["event_kind"] == "gm_token_deposit"
    assert page["items"][0]["credited_usd_nano"] is None
    assert "gm_account" not in response.text
    assert "api_key" not in response.text
    assert page["next_before"] is not None
    older = (
        await client.get(
            "/api/v1/public/treasury-activity",
            params={"before": page["next_before"], "limit": 1},
        )
    ).json()
    assert older["items"][0]["payment_id"] == "payment-1"
    assert older["next_before"] is None
    assert (
        await client.get("/api/v1/public/treasury-activity?limit=101")
    ).status_code == 422

    async with session_maker() as session:
        deposit = await session.scalar(
            select(TreasuryPublicEvent).where(
                TreasuryPublicEvent.payment_id == "payment-2"
            )
        )
        assert deposit is not None
        finalized_id = deposit.id
        confirmation = TreasuryPublicEvent(
            payment_id="payment-2",
            event_kind="gm_credit_purchase",
            state="reconciled",
            finalized_event_id=finalized_id,
            event_at=datetime(2026, 9, 25, tzinfo=UTC),
            policy_revision=3,
            burn_revision=8,
            burn_share_micros=0,
            denominator="released_miner_emission",
            maintenance_bps=25,
            gm_bps=25,
            allocation_bps=25,
            allocated_alpha_rao=9_007_199_254_740_993,
            source_alpha_rao=500,
            route="alpha_to_tao",
            deposit_asset="TAO",
            deposit_amount_atomic=9_007_199_254_740_993,
            credited_usd_nano=3_000_000_000,
            bounty_award_id=None,
            accepted_work_ref=None,
            public_sender="public-sender",
            public_recipient="wrong-recipient",
            block_hash="0xpayment-2",
            extrinsic_index=2,
            event_index=3,
            actor_provenance="gm_reconciler",
            actor_public_id="reconciler-1",
            verification_source="chain_and_provider_reconciliation",
        )
        session.add(confirmation)
        with pytest.raises(DBAPIError, match="lacks matching finalized deposit"):
            await session.commit()
        await session.rollback()
        confirmation.public_recipient = "public-recipient"
        confirmation.burn_share_micros = 1
        session.add(confirmation)
        with pytest.raises(DBAPIError, match="lacks matching finalized deposit"):
            await session.commit()
        await session.rollback()
        confirmation.burn_share_micros = 0
        session.add(confirmation)
        await session.commit()
    reconciled = (await client.get("/api/v1/public/treasury-activity?limit=1")).json()[
        "items"
    ][0]
    assert reconciled["event_kind"] == "gm_credit_purchase"
    assert reconciled["finalized_event_id"] == finalized_id
    assert reconciled["credited_usd_nano"] == "3000000000"

    async with session_maker() as session:
        bounty = TreasuryPublicEvent(
            payment_id="bounty-001",
            event_kind="maintenance_bounty",
            state="chain_finalized",
            finalized_event_id=None,
            event_at=datetime(2026, 9, 25, tzinfo=UTC),
            policy_revision=3,
            burn_revision=8,
            burn_share_micros=0,
            denominator="released_miner_emission",
            maintenance_bps=25,
            gm_bps=25,
            allocation_bps=25,
            allocated_alpha_rao=1000,
            source_alpha_rao=100,
            route="alpha_transfer",
            deposit_asset="SN118_ALPHA",
            deposit_amount_atomic=100,
            credited_usd_nano=None,
            bounty_award_id=None,
            accepted_work_ref=None,
            public_sender="public-sender",
            public_recipient="bounty-recipient",
            block_hash="0xbounty-1",
            extrinsic_index=4,
            event_index=5,
            actor_provenance="bounty_executor",
            actor_public_id="bounty-executor-1",
            verification_source="finalized_chain_rpc",
        )
        session.add(bounty)
        with pytest.raises(DBAPIError):
            await session.commit()
        await session.rollback()
        bounty.bounty_award_id = "award-0001"
        bounty.accepted_work_ref = "accepted-work-0001"
        session.add(bounty)
        await session.commit()
    published_bounty = (
        await client.get("/api/v1/public/treasury-activity?limit=1")
    ).json()["items"][0]
    assert published_bounty["event_kind"] == "maintenance_bounty"
    assert published_bounty["bounty_award_id"] == "award-0001"
    assert published_bounty["accepted_work_ref"] == "accepted-work-0001"
