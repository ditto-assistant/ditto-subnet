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
        "TRUNCATE treasury_funding_reconciliations",
        "UPDATE treasury_funding_reconciliations SET actor='changed'",
    ],
)
async def test_database_prevents_audit_mutation(session_maker, sql):
    await save(session_maker, batch())
    with pytest.raises(DBAPIError, match="append only"):
        async with session_maker() as session, session.begin():
            await session.execute(text(sql))
    async with session_maker() as session:
        assert await session.scalar(select(TreasuryFundingReconciliation.purchase_id))


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


async def test_endpoint_requires_service_auth_and_preserves_operator_header(
    app, client, session_maker
):
    from dataclasses import replace

    from ditto.api_server.dependencies import get_session

    token = "funding-test-service-token-at-least-32-characters"
    app.state.config = replace(app.state.config, admin_api_token=token)

    async def sessions():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    payload = submit(batch()).model_dump(mode="json")
    payload["actor"] = "forged@example.com"
    url = "/api/v1/admin/treasury-funding"
    assert (
        await client.post(
            url, headers={"X-Admin-Actor": "forged@example.com"}, json=payload
        )
    ).status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post(url, headers=headers, json=payload)).status_code == 422
    response = await client.post(
        url, headers={**headers, "X-Admin-Actor": "signed-in@example.com"}, json=payload
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    async with session_maker() as session:
        row = await session.scalar(select(TreasuryFundingReconciliation))
        assert row.actor == "signed-in@example.com"


def funding_migration():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/2026_10_27_zzzzzz_add_treasury_funding_reconciliations.py"
    )
    spec = importlib.util.spec_from_file_location("funding_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_empty_migration_downgrade_and_upgrade_roundtrip(engine):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = funding_migration()

    def roundtrip(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
            assert (
                connection.scalar(
                    text("SELECT to_regclass('treasury_funding_reconciliations')")
                )
                is None
            )
            module.upgrade()
            assert connection.scalar(
                text("SELECT to_regclass('treasury_funding_reconciliations')")
            )

    async with engine.begin() as connection:
        await connection.run_sync(roundtrip)


async def test_migration_downgrade_preserves_retained_history(engine, session_maker):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    await save(session_maker, batch())
    module = funding_migration()

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    with pytest.raises(RuntimeError, match="audit history"):
        async with engine.begin() as connection:
            await connection.run_sync(downgrade)
    async with session_maker() as session:
        assert await session.scalar(select(TreasuryFundingReconciliation.purchase_id))
