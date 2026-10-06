"""Forecasts cannot masquerade as active funding or leak billing identity."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["observe", "enforce", "pause"])
@pytest.mark.parametrize("fault", ["none", "checksum", "signature", "allocation"])
async def test_public_runtime_exposes_verified_control_without_funding_or_private_audit(
    app, client, session_maker, monkeypatch, mode, fault
):
    from ditto.api_server.treasury_runtime import canonical_settings
    from ditto.db.models import TreasuryRuntimeRevision
    from ditto.tests.api_server.test_treasury_runtime import payload, seed_shadow

    app.state.session_maker = session_maker
    await seed_shadow(session_maker)
    raw = payload(mode)["settings"]
    monkeypatch.setattr(
        "ditto.api_server.treasury_runtime.verify_public_signature",
        lambda *_: fault != "signature",
    )
    async with session_maker() as session:
        row = TreasuryRuntimeRevision(
            parent_revision=0,
            settings=raw,
            checksum="0" * 64 if fault == "checksum" else canonical_settings(raw),
            actor="PRIVATE-ACTOR",
            reason="PRIVATE-REASON",
        )
        session.add(row)
        if fault == "allocation":
            policy_row = await session.scalar(select(TreasurySettingsRevision))
            changed = {**policy_row.settings, "treasury_coldkey": "5" + "a" * 47}
            session.add(
                TreasurySettingsRevision(
                    parent_revision=policy_row.revision,
                    settings=changed,
                    checksum=canonical_settings(changed),
                    actor="PRIVATE-SECOND-ACTOR",
                    reason="PRIVATE-CHANGED-ALLOCATION",
                )
            )
        await session.commit()
        revision, stored_settings, checksum = row.revision, row.settings, row.checksum
    response = await client.get("/api/v1/public/treasury-allocation")
    assert response.status_code == 200, response.text
    body = response.json()
    control = body["runtime"]
    assert control["revision"] == revision
    invalid = fault in {"checksum", "signature"}
    assert control["mode"] == ("unavailable" if invalid else mode)
    assert control["activation_epoch"] == (None if invalid else raw["activation_epoch"])
    assert control["allocation_matches"] == (None if invalid else fault != "allocation")
    assert set(control) == {
        "revision",
        "mode",
        "activation_epoch",
        "allocation_matches",
    }
    # Recording control cannot become chain routing, funds or sweep proof.
    assert body["routing_status"] == body["sweep_status"] == "not_activated"
    assert body["effective_service_share"] == 0
    assert "PRIVATE" not in response.text
    assert "signature" not in response.text
    async with session_maker() as session:
        preserved = await session.get(TreasuryRuntimeRevision, revision)
        assert preserved.settings == stored_settings
        assert preserved.checksum == checksum
