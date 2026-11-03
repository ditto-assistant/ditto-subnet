"""Append-only private accounting observations. No payment or public proof authority."""

from datetime import UTC, datetime

from sqlalchemy import select, text

from ditto.api_models.treasury_funding import FundingBatch, FundingRecord, checksum
from ditto.api_models.treasury_settings import TreasurySettings
from ditto.db.models import TreasuryFundingReconciliation as Reconciliation
from ditto.db.models import TreasurySettingsRevision


async def holding_wallet(session):
    latest = await session.scalar(
        select(TreasurySettingsRevision)
        .order_by(TreasurySettingsRevision.revision.desc())
        .limit(1)
    )
    if latest is None:
        raise ValueError("treasury wallet policy is not configured")
    settings = TreasurySettings.model_validate(latest.settings)
    wallets = [
        b.holding_coldkey for b in settings.service_buckets if b.bucket_id == "gm"
    ]
    if len(wallets) != 1 or wallets[0] is None:
        raise ValueError("one GM holding wallet is required")
    return wallets[0]


def movements(record):
    return [
        record.payment,
        *([record.preceding_sale] if record.preceding_sale else []),
        *record.earlier_conversions,
    ]


async def preview(session, batch: FundingBatch):
    if batch.holding_wallet != await holding_wallet(session):
        raise ValueError("reconciliation sender differs from the GM holding wallet")
    if any(
        e.occurred_at > datetime.now(UTC)
        for r in batch.records
        for e in [r.purchase, *movements(r)]
    ):
        raise ValueError("future observations cannot be reconciled")
    existing = list(await session.scalars(select(Reconciliation).limit(5001)))
    if len(existing) > 5000:
        raise ValueError("reconciliation history exceeds bounded validation window")
    facts, payments = {}, {}
    for row in existing:
        record = FundingRecord.model_validate(row.payload["record"])
        payments[record.payment.reference] = record.purchase.purchase_id
        for event in movements(record):
            facts[(row.payload["holding_wallet"], event.reference)] = event.model_dump(
                mode="json"
            )
    for record in batch.records:
        owner = payments.get(record.payment.reference)
        if owner is not None and owner != record.purchase.purchase_id:
            raise ValueError("payment already belongs to another purchase")
        for event in movements(record):
            key = (batch.holding_wallet, event.reference)
            data = event.model_dump(mode="json")
            if key in facts and facts[key] != data:
                raise ValueError("an existing chain observation changed")
            facts[key] = data
        payload = {
            "holding_wallet": batch.holding_wallet,
            "record": record.model_dump(mode="json"),
        }
        previous = await session.get(Reconciliation, record.purchase.purchase_id)
        if previous and previous.digest != checksum(payload):
            raise ValueError("provider purchase conflicts with immutable history")
    return {
        "confirmation_digest": batch.digest,
        "confirmation": f"RECORD GM FUNDING HISTORY {batch.digest}",
        "purchases": len(batch.records),
        "credited_usd_cents": sum(r.purchase.credited_usd_cents for r in batch.records),
        "verification_level": "operator_reconciled",
        "spending_authority": "none",
    }


async def record_batch(session, submission, actor):
    await session.execute(text("SELECT pg_advisory_xact_lock(118,2750)"))
    plan = await preview(session, submission.batch)
    if (
        submission.confirmation_digest != plan["confirmation_digest"]
        or submission.confirmation != plan["confirmation"]
    ):
        raise ValueError("exact reconciliation confirmation required")
    inserted = 0
    for record in submission.batch.records:
        if await session.get(Reconciliation, record.purchase.purchase_id):
            continue
        payload = {
            "holding_wallet": submission.batch.holding_wallet,
            "record": record.model_dump(mode="json"),
        }
        session.add(
            Reconciliation(
                purchase_id=record.purchase.purchase_id,
                payment_reference=record.payment.reference,
                payload=payload,
                digest=checksum(payload),
                actor=actor,
                reason=submission.batch.reason,
            )
        )
        inserted += 1
    await session.flush()
    return {
        **plan,
        "inserted": inserted,
        "replayed": len(submission.batch.records) - inserted,
    }


async def history(session):
    rows = list(
        await session.scalars(
            select(Reconciliation)
            .order_by(Reconciliation.recorded_at.desc(), Reconciliation.purchase_id)
            .limit(501)
        )
    )
    truncated = len(rows) > 500
    rows = rows[:500]
    items = [
        {
            **row.payload,
            "actor": row.actor,
            "recorded_at": row.recorded_at.isoformat(),
            "verification_level": "operator_reconciled",
        }
        for row in rows
    ]
    items.sort(
        key=lambda item: datetime.fromisoformat(
            item["record"]["purchase"]["occurred_at"]
        ),
        reverse=True,
    )
    sales = {}
    for item in items:
        record = FundingRecord.model_validate(item["record"])
        for event in movements(record):
            if event.kind in {"sale", "swap"}:
                sales[(item["holding_wallet"], event.reference)] = event
    return {
        "items": items,
        "truncated": truncated,
        "totals_scope": "returned_records",
        "credited_usd_cents": sum(
            i["record"]["purchase"]["credited_usd_cents"] for i in items
        ),
        "tao_paid_rao": str(
            sum(int(i["record"]["payment"]["input_amount_atomic"]) for i in items)
        ),
        "distinct_conversions": len(sales),
        "automatic_topups_enabled": False,
        "spending_authority": "none",
    }
