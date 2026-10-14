"""The Gamma ledger explains every collector outflow or reports it unattributed."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ditto.api_server.treasury_ledger import (
    TreasuryLedgerSampler,
    ledger_view,
    record_sample,
)
from ditto.chain.treasury_ledger_read import (
    TreasuryLedgerHolding,
    TreasuryLedgerReading,
)
from ditto.db.models import TreasuryPublicEvent, TreasurySettingsRevision

COLLECTOR = "5" + "c" * 47
HOTKEY = "5" + "h" * 47
OWNER = "5" + "o" * 47
ALPHA = 1_000_000_000
START = datetime(2026, 10, 14, tzinfo=UTC)
EPOCH = timedelta(minutes=72)


def reading(epoch, total, *, emission=100 * ALPHA, coldkey=COLLECTOR, locked=0):
    return TreasuryLedgerReading(
        netuid=118,
        epoch_index=epoch,
        block_number=epoch * 360,
        block_hash=f"0x{epoch:064x}",
        block_timestamp_ms=int((START + EPOCH * epoch).timestamp() * 1000),
        collector_hotkey=HOTKEY,
        collector_coldkey=coldkey,
        collector_uid=21,
        collector_incentive=0.75,
        collector_emission_rao=emission,
        collector_total_rao=total,
        collector_locked_rao=locked,
        collector_available_rao=total - locked,
        collector_lock_perpetual=True if locked else None,
        owner_hotkey=OWNER,
        owner_conviction_rao=102_000 * ALPHA,
        most_convicted_hotkey=OWNER,
        holdings=(TreasuryLedgerHolding("gm", "5" + "g" * 47, 200, 300),),
    )


def receipt(epoch, amount, *, sender=COLLECTOR, offset=timedelta(minutes=10)):
    return TreasuryPublicEvent(
        payment_id=f"payment-{epoch}-{amount}",
        bucket_id="gm",
        event_kind="service_distribution",
        state="chain_finalized",
        event_at=START + EPOCH * epoch - offset,
        policy_revision=4,
        burn_revision=12,
        burn_share_micros=0,
        denominator="collector_liquid_emission",
        maintenance_bps=0,
        gm_bps=0,
        allocation_bps=6500,
        allocated_alpha_rao=amount,
        source_alpha_rao=amount,
        route="alpha_transfer",
        deposit_asset="SN118_ALPHA",
        deposit_amount_atomic=amount,
        public_sender=sender,
        public_recipient="5" + "g" * 47,
        block_hash=f"0xreceipt{epoch}{amount}",
        extrinsic_index=1,
        event_index=2,
        actor_provenance="treasury_observer",
        actor_public_id="receipt-ingress-v1",
        verification_source="finalized_chain_rpc",
    )


async def seed(session_maker, readings, receipts=()):
    async with session_maker() as session, session.begin():
        for item in readings:
            await record_sample(session, item)
        session.add_all(list(receipts))


async def view(session_maker, limit=10):
    from ditto.api_models.treasury_ledger import TreasuryLedgerSamplerStatus

    async with session_maker() as session:
        return await ledger_view(
            session,
            netuid=118,
            limit=limit,
            sampler=TreasuryLedgerSamplerStatus(enabled=True, state="running"),
        )


@pytest.mark.asyncio
async def test_one_sample_per_epoch(session_maker):
    async with session_maker() as session, session.begin():
        assert await record_sample(session, reading(1, 10 * ALPHA))
        assert not await record_sample(session, reading(1, 99 * ALPHA))
    ledger = await view(session_maker)
    assert ledger.current is not None
    assert ledger.current.total_rao == 10 * ALPHA
    assert ledger.intervals == []


@pytest.mark.asyncio
async def test_receipts_attribute_outflow_and_the_rest_is_unattributed(session_maker):
    await seed(
        session_maker,
        [
            reading(1, 1_000 * ALPHA),
            # Earned 100, held: the balance grew by exactly what was earned.
            reading(2, 1_100 * ALPHA),
            # Earned 100, 60 left with a verified receipt.
            reading(3, 1_140 * ALPHA),
            # Earned 100, 150 left with no receipt.
            reading(4, 1_090 * ALPHA),
            # Skipped an epoch: earnings are extrapolated and flagged.
            reading(6, 1_290 * ALPHA),
        ],
        [
            receipt(3, 60 * ALPHA),
            # Another sender's payment never attributes the collector.
            receipt(4, 150 * ALPHA, sender="5" + "x" * 47),
        ],
    )
    ledger = await view(session_maker)
    by_epoch = {item.epoch_index: item for item in ledger.intervals}
    assert by_epoch[2].outflow_rao == 0
    assert by_epoch[3].outflow_rao == 60 * ALPHA
    assert by_epoch[3].attributed_rao == 60 * ALPHA
    assert by_epoch[3].unattributed_rao == 0
    assert len(by_epoch[3].receipt_ids) == 1
    assert by_epoch[4].outflow_rao == 150 * ALPHA
    assert by_epoch[4].unattributed_rao == 150 * ALPHA
    assert by_epoch[6].estimated and by_epoch[6].earned_rao == 200 * ALPHA
    assert by_epoch[6].outflow_rao == 0
    assert ledger.totals.window_epochs == 5
    assert ledger.totals.earned_rao == 500 * ALPHA
    assert ledger.totals.attributed_rao == 60 * ALPHA
    assert ledger.totals.unattributed_rao == 150 * ALPHA
    assert ledger.current is not None
    assert ledger.current.owner_holds_most_conviction is True
    assert ledger.current.lock_status == "none"


@pytest.mark.asyncio
async def test_locked_balance_and_collector_rotation(session_maker):
    await seed(
        session_maker,
        [
            reading(1, 1_000 * ALPHA),
            # A new collector account is not an outflow of the old one.
            reading(2, 5 * ALPHA, coldkey="5" + "n" * 47, locked=5 * ALPHA),
        ],
    )
    ledger = await view(session_maker)
    assert ledger.intervals == []
    assert ledger.current is not None
    assert ledger.current.lock_status == "perpetual"
    assert ledger.current.available_rao == 0


@pytest.mark.asyncio
async def test_sampler_skips_without_collector_then_records(app, session_maker):
    sampler = TreasuryLedgerSampler(app_state=app.state, session_maker=session_maker)
    assert not await sampler.tick()
    assert sampler.public_status().state == "not_configured"
    async with session_maker() as session, session.begin():
        session.add(
            TreasurySettingsRevision(
                parent_revision=0,
                settings={
                    "allocation_version": 2,
                    "treasury_hotkey": HOTKEY,
                    "treasury_coldkey": COLLECTOR,
                    "service_buckets": [
                        {
                            "bucket_id": "gm",
                            "purpose": "GM inference credits",
                            "allocation_bps": 6500,
                            "holding_coldkey": "5" + "g" * 47,
                        }
                    ],
                },
                checksum="a" * 64,
                reason="configure the Gamma collector for ledger tests",
                actor="operator",
            )
        )
    seen = []

    async def fake_read(policy):
        seen.append(
            (
                policy.treasury_hotkey,
                policy.treasury_coldkey,
                [bucket.holding_coldkey for bucket in policy.service_buckets],
            )
        )
        return reading(7, 10 * ALPHA)

    sampler.read = fake_read  # type: ignore[method-assign]
    assert await sampler.tick()
    assert not await sampler.tick()
    assert seen == [(HOTKEY, COLLECTOR, ["5" + "g" * 47])] * 2
    status = sampler.public_status()
    assert status.state == "running" and status.last_sampled_at is not None


@pytest.mark.asyncio
async def test_public_and_admin_routes(app, client, session_maker):
    app.state.session_maker = session_maker
    app.state.config = replace(
        app.state.config,
        admin_api_token="ledger-admin-token-at-least-32-characters-long",
    )
    empty = await client.get("/api/v1/public/treasury-ledger")
    assert empty.status_code == 200, empty.text
    assert empty.json()["current"] is None
    await seed(session_maker, [reading(1, 1_000 * ALPHA), reading(2, 1_100 * ALPHA)])
    public = await client.get("/api/v1/public/treasury-ledger?limit=5")
    body = public.json()
    assert body["current"]["epoch_index"] == 2
    assert body["intervals"][0]["earned_rao"] == 100 * ALPHA
    assert (await client.get("/api/v1/admin/treasury-ledger")).status_code == 401
    admin = await client.get(
        "/api/v1/admin/treasury-ledger",
        headers={
            "Authorization": "Bearer ledger-admin-token-at-least-32-characters-long"
        },
    )
    assert admin.status_code == 200
    assert admin.json()["totals"] == body["totals"]
