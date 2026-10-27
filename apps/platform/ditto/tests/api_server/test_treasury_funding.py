"""Real PostgreSQL reconciliation durability, conflicts and accounting totals."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from ditto.api_models.treasury_funding import GM_RECIPIENT, FundingBatch, FundingSubmit
from ditto.api_server import treasury_funding as funding
from ditto.db.models import TreasuryFundingReconciliation

pytestmark = pytest.mark.asyncio
WALLET = "5" + "a" * 47


@pytest.fixture(autouse=True)
def wallet(monkeypatch):
    async def read(_session):
        return WALLET

    monkeypatch.setattr(funding, "holding_wallet", read)


def batch():
    return FundingBatch.model_validate(
        {
            "holding_wallet": WALLET,
            "reason": "Reconcile observed provider purchase",
            "records": [
                {
                    "purchase": {
                        "purchase_id": str(uuid4()),
                        "occurred_at": "2026-01-01T00:00:30Z",
                        "credited_usd_cents": 25000,
                    },
                    "payment": {
                        "reference": "9000000-3",
                        "kind": "payment",
                        "occurred_at": "2026-01-01T00:00:00Z",
                        "input_asset": "TAO",
                        "input_amount_atomic": "2000000000",
                        "recipient": GM_RECIPIENT,
                    },
                    "preceding_sale": {
                        "reference": "8999990-1",
                        "kind": "sale",
                        "occurred_at": "2025-12-31T23:58:00Z",
                        "input_asset": "SN118_ALPHA",
                        "input_amount_atomic": "500000000000",
                        "output_asset": "TAO",
                        "output_amount_atomic": "2100000000",
                    },
                }
            ],
        }
    )


def submit(value):
    return FundingSubmit(
        batch=value,
        confirmation_digest=value.digest,
        confirmation=f"RECORD GM FUNDING HISTORY {value.digest}",
    )


async def save(maker, value):
    async with maker() as session, session.begin():
        return await funding.record_batch(
            session, submit(value), "operator@example.com"
        )


async def test_concurrent_replay_is_idempotent_and_audited(session_maker):
    value = batch()
    results = await asyncio.gather(
        save(session_maker, value), save(session_maker, value)
    )
    assert sorted(r["inserted"] for r in results) == [0, 1]
    async with session_maker() as session:
        row = await session.scalar(select(TreasuryFundingReconciliation))
        assert row.actor == "operator@example.com"
        assert row.reason == value.reason
        result = await funding.history(session)
    assert result["credited_usd_cents"] == 25000
    assert result["tao_paid_rao"] == "2000000000"
    assert result["automatic_topups_enabled"] is False


async def test_shared_sale_counted_once_and_existing_balance_allowed(session_maker):
    raw = batch().model_dump(mode="json")
    second = deepcopy(raw["records"][0])
    second["purchase"]["purchase_id"] = str(uuid4())
    second["payment"]["reference"] = "9000000-4"
    raw["records"].append(second)
    await save(session_maker, FundingBatch.model_validate(raw))
    async with session_maker() as session:
        result = await funding.history(session)
    assert result["distinct_conversions"] == 1
    assert result["tao_paid_rao"] == "4000000000"
    assert result["credited_usd_cents"] == 50000


@pytest.mark.parametrize("change", ["purchase", "payment", "sale"])
async def test_conflicting_facts_refused(session_maker, change):
    value = batch()
    await save(session_maker, value)
    raw = value.model_dump(mode="json")
    row = raw["records"][0]
    if change == "purchase":
        row["purchase"]["credited_usd_cents"] += 1
    elif change == "payment":
        row["purchase"]["purchase_id"] = str(uuid4())
    else:
        row["purchase"]["purchase_id"] = str(uuid4())
        row["payment"]["reference"] = "9000000-4"
        row["preceding_sale"]["output_amount_atomic"] = "2100000001"
    with pytest.raises(ValueError):
        await save(session_maker, FundingBatch.model_validate(raw))


async def test_exact_confirmation_and_wallet_required(session_maker):
    value = batch()
    wrong = submit(value).model_copy(update={"confirmation": "yes"})
    async with session_maker() as session, session.begin():
        with pytest.raises(ValueError, match="confirmation"):
            await funding.record_batch(session, wrong, "operator@example.com")
    raw = value.model_dump(mode="json")
    raw["holding_wallet"] = "5" + "b" * 47
    with pytest.raises(ValueError, match="wallet"):
        await save(session_maker, FundingBatch.model_validate(raw))


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM treasury_funding_reconciliations",
        "UPDATE treasury_funding_reconciliations SET actor='changed'",
    ],
)
async def test_database_prevents_audit_mutation(session_maker, sql):
    await save(session_maker, batch())
    with pytest.raises(DBAPIError, match="append only"):
        async with session_maker() as session, session.begin():
            await session.execute(text(sql))


@pytest.mark.parametrize("field,value", [("status", "pending"), ("method", "stripe")])
async def test_pending_or_nonchain_purchases_refused(field, value):
    raw = batch().model_dump(mode="json")
    raw["records"][0]["purchase"][field] = value
    with pytest.raises(ValidationError):
        FundingBatch.model_validate(raw)


async def test_unknown_fields_do_not_change_digest():
    value = batch()
    raw = value.model_dump(mode="json")
    raw["untrusted_proof"] = "verified"
    raw["records"][0]["purchase"]["unknown"] = True
    assert FundingBatch.model_validate(raw).digest == value.digest
