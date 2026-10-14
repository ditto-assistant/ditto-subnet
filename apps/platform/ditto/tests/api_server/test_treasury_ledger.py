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
from ditto.db.models import (
    TreasuryPublicEvent,
    TreasurySettingsRevision,
    TreasuryVerifiedReceipt,
)

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


def public_event(epoch, amount):
    return TreasuryPublicEvent(
        payment_id=f"payment-{epoch}-{amount}",
        bucket_id="gm",
        event_kind="service_distribution",
        state="chain_finalized",
        event_at=START + EPOCH * epoch,
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
        public_sender=COLLECTOR,
        public_recipient="5" + "g" * 47,
        block_hash=f"0xpublic{epoch}{amount}",
        extrinsic_index=1,
        event_index=2,
        actor_provenance="treasury_observer",
        actor_public_id="receipt-ingress-v1",
        verification_source="finalized_chain_rpc",
    )


def receipt(epoch, amount, *, sender=COLLECTOR, asset="SN118_ALPHA", public=None):
    """A verified distribution finalized inside ``epoch``'s interval."""
    block = epoch * 360 - 10
    return TreasuryVerifiedReceipt(
        receipt_id=f"receipt-{epoch}-{amount}-{sender[:4]}-{asset}",
        request_digest="d" * 64,
        stage="service_distribution",
        epoch_index=epoch,
        settings_revision=1,
        policy_digest="p" * 64,
        collector_policy_digest="c" * 64,
        bucket_id="gm",
        source_block=block - 5,
        block_hash=f"0x{block:064x}{sender[:2]}{asset}",
        extrinsic_index=1,
        event_index=2,
        amount_atomic=amount,
        proof={"block": block, "sender": sender, "asset": asset},
        published=public is not None,
        public_event_id=public.id if public is not None else None,
        reason="verified collector distribution",
        actor="operator",
    )


async def seed(session_maker, readings, receipts=()):
    async with session_maker() as session, session.begin():
        for item in readings:
            await record_sample(session, item)
        if receipts:
            session.add(
                TreasurySettingsRevision(
                    revision=1,
                    parent_revision=0,
                    settings={},
                    checksum="a" * 64,
                    reason="seed the ledger test policy revision",
                    actor="operator",
                )
            )
            await session.flush()
            for item in receipts:
                if callable(item):
                    event = public_event(3, 60 * ALPHA)
                    session.add(event)
                    await session.flush()
                    item = item(event)
                session.add(item)


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
            # Published: attributes and exposes its public event id.
            lambda event: receipt(3, 60 * ALPHA, public=event),
            # Another sender's payment never attributes the collector.
            receipt(4, 150 * ALPHA, sender="5" + "x" * 47),
            # Another subnet's alpha never attributes this balance.
            receipt(4, 150 * ALPHA, asset="SN28_ALPHA"),
            # Unpublished, and larger than the epoch's outflow: it attributes
            # by amount only, capped at what the interval measured.
            receipt(6, 5 * ALPHA),
        ],
    )
    ledger = await view(session_maker)
    by_epoch = {item.epoch_index: item for item in ledger.intervals}
    assert by_epoch[2].outflow_rao == 0
    assert by_epoch[3].outflow_rao == 60 * ALPHA
    assert by_epoch[3].attributed_rao == 60 * ALPHA
    assert by_epoch[3].unattributed_rao == 0
    assert by_epoch[3].receipt_count == len(by_epoch[3].receipt_ids) == 1
    assert by_epoch[4].outflow_rao == 150 * ALPHA
    assert by_epoch[4].unattributed_rao == 150 * ALPHA
    assert by_epoch[4].receipt_count == 0
    assert by_epoch[6].estimated and by_epoch[6].earned_rao == 200 * ALPHA
    assert by_epoch[6].outflow_rao == 0
    assert by_epoch[6].attributed_rao == 0
    assert by_epoch[6].receipt_count == 1 and by_epoch[6].receipt_ids == []
    assert ledger.totals.window_epochs == 5
    assert ledger.totals.earned_rao == 500 * ALPHA
    assert ledger.totals.attributed_rao == 60 * ALPHA
    assert ledger.totals.unattributed_rao == 150 * ALPHA
    assert ledger.current is not None
    assert ledger.current.owner_holds_most_conviction is True
    assert ledger.current.lock_status == "none"


@pytest.mark.asyncio
async def test_outflow_at_the_dust_threshold_is_reported(session_maker):
    from ditto.api_server.treasury_ledger import DUST_RAO

    await seed(
        session_maker,
        [reading(1, 1_000 * ALPHA), reading(2, 1_100 * ALPHA - DUST_RAO)],
    )
    (interval,) = (await view(session_maker)).intervals
    assert interval.outflow_rao == interval.unattributed_rao == DUST_RAO


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
    assert ledger.totals.window_epochs == 0
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

    async def fake_read(policy, should_read):
        seen.append(
            (
                policy.treasury_hotkey,
                policy.treasury_coldkey,
                [bucket.holding_coldkey for bucket in policy.service_buckets],
            )
        )
        if not await should_read(7):
            return None
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
    # Rao amounts are decimal strings, safe for JavaScript clients.
    assert body["intervals"][0]["earned_rao"] == str(100 * ALPHA)
    assert body["current"]["total_rao"] == str(1_100 * ALPHA)
    assert (await client.get("/api/v1/admin/treasury-ledger")).status_code == 401
    admin = await client.get(
        "/api/v1/admin/treasury-ledger",
        headers={
            "Authorization": "Bearer ledger-admin-token-at-least-32-characters-long"
        },
    )
    assert admin.status_code == 200
    assert admin.json()["totals"] == body["totals"]
