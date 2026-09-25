"""Treasury feed must publish exact receipts without private GM or signer data."""

from datetime import UTC, datetime

import pytest

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
                    event_kind="gm_credit_purchase",
                    state="chain_finalized",
                    event_at=datetime(2026, 9, 25, tzinfo=UTC),
                    policy_revision=3,
                    burn_revision="8",
                    denominator="released_miner_emission",
                    allocation_bps=25,
                    allocated_alpha_rao=1000,
                    route="alpha_to_tao",
                    asset="TAO",
                    gross_amount_atomic=99,
                    realized_amount_atomic=None,
                    public_sender="public-sender",
                    public_recipient="public-recipient",
                    block_hash=f"0x{payment_id}",
                    extrinsic_index=2,
                    event_index=3,
                    actor_provenance="authenticated_operator",
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
