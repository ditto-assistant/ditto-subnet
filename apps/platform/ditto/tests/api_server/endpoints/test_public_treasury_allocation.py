"""Forecasts cannot masquerade as active funding or leak billing identity."""

from datetime import UTC, datetime

import pytest

from ditto.api_server.admin_activity import public_details
from ditto.db.models import BurnSettingsRevision, TreasurySettingsRevision


@pytest.mark.asyncio
async def test_service_first_forecast_is_public_but_inactive(
    app, client, session_maker
):
    app.state.session_maker = session_maker
    policy = {
        "allocation_version": 2,
        "treasury_hotkey": ("5" + "a" * 47),
        "treasury_coldkey": ("5" + "b" * 47),
        "sweep_interval_hours": 12,
        "service_buckets": [
            {
                "bucket_id": "gm_credits",
                "purpose": "GM inference credits",
                "allocation_bps": 1000,
                "holding_coldkey": ("5" + "c" * 47),
                "service_account_ref": "PRIVATE-GM-ACCOUNT",
                "payee_rules": [
                    {
                        "rule_id": "gm_treasury",
                        "label": "GM credit payment",
                        "recipient_coldkey": ("5" + "e" * 47),
                    }
                ],
            }
        ],
    }
    async with session_maker() as session:
        session.add(
            TreasurySettingsRevision(
                parent_revision=0,
                settings=policy,
                checksum="a" * 64,
                reason="PRIVATE-REASON",
                actor="PRIVATE-ACTOR",
            )
        )
        session.add(
            BurnSettingsRevision(
                parent_revision=0,
                scope="*",
                settings={"burn_share": 1.0},
                checksum="b" * 64,
                reason="operator full burn",
                actor="operator",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    result = await client.get("/api/v1/public/treasury-allocation")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["forecast_service_share"] == 0.1
    assert body["forecast_burn_share"] == 0.9
    assert body["forecast_miner_share"] == 0
    assert body["effective_service_share"] == 0
    assert body["routing_status"] == body["sweep_status"] == "not_activated"
    assert body["buckets"][0]["holding_coldkey"] == ("5" + "c" * 47)
    assert body["sweep_interval_hours"] == 12
    assert "PRIVATE" not in result.text
    audit = public_details("/api/v1/admin/treasury-settings", {"settings": policy})
    assert audit["settings"]["service_buckets"][0]["holding_coldkey"] == (
        "5" + "c" * 47
    )
    assert "service_account_ref" not in str(audit)


@pytest.mark.asyncio
async def test_default_is_not_a_claim_of_ten_percent_funding(
    app, client, session_maker
):
    app.state.session_maker = session_maker
    result = await client.get("/api/v1/public/treasury-allocation")
    assert result.status_code == 200
    body = result.json()
    assert body["service_bps"] == 0
    assert body["collector_hotkey"] is None
    assert body["buckets"] == []


@pytest.mark.asyncio
async def test_legacy_forecast_retains_released_share_denominator(
    app, client, session_maker
):
    app.state.session_maker = session_maker
    async with session_maker() as session:
        session.add(
            TreasurySettingsRevision(
                parent_revision=0,
                settings={
                    "gm_bps": 100,
                    "treasury_hotkey": "legacy private arbitrary text",
                    "treasury_coldkey": "legacy private coldkey text",
                    "gm_account_ref": "private-account",
                },
                checksum="c" * 64,
                reason="legacy economic policy",
                actor="operator",
            )
        )
        session.add(
            BurnSettingsRevision(
                parent_revision=0,
                scope="*",
                settings={"burn_share": 1.0},
                checksum="d" * 64,
                reason="full burn",
                actor="operator",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    body = (await client.get("/api/v1/public/treasury-allocation")).json()
    assert body["denominator"] == "released_miner_emission"
    assert body["forecast_service_share"] == 0
    assert body["forecast_burn_share"] == 1
    assert body["collector_hotkey"] is None
    assert body["collector_coldkey"] is None
    audit = public_details(
        "/api/v1/admin/treasury-settings",
        {"settings": {"treasury_hotkey": "legacy private arbitrary text"}},
    )
    assert "legacy private" not in str(audit)
