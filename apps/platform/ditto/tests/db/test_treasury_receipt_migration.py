"""Real-PG empty rollback and immutable financial-history refusal controls."""

import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

PARENT = "9c41d7e2b6a3"
HEAD = "b391f0e8c2d6"


def alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=check,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


@pytest.fixture(autouse=True)
async def receipt_migration_schema(engine: AsyncEngine):
    """Exercise this historical revision and restore the current worker schema.

    Alembic commits each revision separately. A refusal in this revision can
    follow a successful rollback of a newer empty table, so leaving the worker
    at HEAD poisons the next test's current-head reset. Keep the historical
    refusal assertions unchanged and restore the actual head even on failure.
    """
    assert engine.dialect.name == "postgresql"
    try:
        alembic("downgrade", HEAD)
        yield
    finally:
        alembic("upgrade", "head")


async def schema(engine: AsyncEngine) -> list[tuple]:
    async with engine.connect() as conn:
        return [
            tuple(row)
            for row in (
                await conn.execute(
                    text(
                        "SELECT 'constraint', conname::text, "
                        "pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                        "conrelid IN ('treasury_public_events'::regclass, "
                        "'treasury_verified_receipts'::regclass) UNION ALL "
                        "SELECT 'trigger', tgname::text, pg_get_triggerdef(oid) "
                        "FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN "
                        "('treasury_public_events'::regclass, "
                        "'treasury_verified_receipts'::regclass) UNION ALL "
                        "SELECT 'index', indexname::text, indexdef FROM pg_indexes "
                        "WHERE schemaname='public' AND tablename IN "
                        "('treasury_public_events', 'treasury_verified_receipts') "
                        "ORDER BY 1, 2, 3"
                    )
                )
            ).all()
        ]


async def public_fact(engine: AsyncEngine, kind: str = "legacy") -> None:
    event = "vendor_payment" if kind == "vendor" else "gm_token_deposit"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO treasury_public_events "
                "(payment_id,event_kind,state,event_at,policy_revision,burn_revision,"
                "burn_share_micros,denominator,maintenance_bps,gm_bps,allocation_bps,"
                "allocated_alpha_rao,source_alpha_rao,route,deposit_asset,"
                "deposit_amount_atomic,public_sender,public_recipient,block_hash,"
                "extrinsic_index,event_index,actor_provenance,actor_public_id,"
                "verification_source,bucket_id,policy_digest,epoch_index) VALUES "
                "('retained-fact',:event,'chain_finalized',now(),1,0,0,"
                "'miner_emission',0,0,0,0,1,'alpha_transfer','SN118_ALPHA',1,"
                "'sender','recipient','block-hash',0,0,'treasury_signer',"
                "'signer','finalized_chain_rpc',:bucket,:digest,:epoch)"
            ),
            {
                "event": event,
                "bucket": "gamma" if kind == "bucket" else None,
                "digest": "a" * 64 if kind == "digest" else None,
                "epoch": 3 if kind == "epoch" else None,
            },
        )


@pytest.mark.parametrize("legacy", [False, True])
async def test_empty_schema_roundtrip_preserves_legacy_facts(
    engine: AsyncEngine, legacy: bool
):
    if legacy:
        await public_fact(engine)
    before = await schema(engine)
    try:
        alembic("downgrade", PARENT)
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT to_regclass('treasury_verified_receipts')")
                )
                is None
            )
            assert await conn.scalar(
                text("SELECT count(*) FROM treasury_public_events")
            ) == int(legacy)
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                == PARENT
            )
        alembic("upgrade", HEAD)
        assert await schema(engine) == before
    finally:
        alembic("upgrade", "head")


@pytest.mark.parametrize("kind", ["receipt", "vendor", "bucket", "digest", "epoch"])
async def test_retained_financial_history_refuses_before_any_ddl(
    engine: AsyncEngine, kind: str
):
    if kind == "receipt":
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO treasury_settings_revisions "
                    "(revision,parent_revision,settings,checksum,reason,actor) "
                    "VALUES (1,0,'{}',:digest,'retained policy','test-actor')"
                ),
                {"digest": "a" * 64},
            )
            await conn.execute(
                text(
                    "INSERT INTO treasury_verified_receipts "
                    "(receipt_id,request_digest,stage,policy_digest,collector_policy_digest,"
                    "bucket_id,block_hash,reason,actor,epoch_index,source_block,amount_atomic,"
                    "settings_revision,extrinsic_index,event_index,proof,published) "
                    "VALUES "
                    "('retained-receipt','request','service_distribution','policy','collector',"
                    "'gamma','block','retained receipt','actor',1,110,40,1,0,0,"
                    "'{}',false)"
                )
            )
    else:
        await public_fact(engine, kind)
    table = (
        "treasury_verified_receipts" if kind == "receipt" else "treasury_public_events"
    )
    async with engine.connect() as conn:
        original = await conn.scalar(
            text(f"SELECT row_to_json(r)::text FROM {table} r")
        )
    before = await schema(engine)
    result = alembic("downgrade", PARENT, check=False)
    assert result.returncode != 0
    assert "retained treasury financial history" in result.stderr
    assert await schema(engine) == before
    async with engine.connect() as conn:
        assert (
            await conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        )
        assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        assert (
            await conn.scalar(text(f"SELECT row_to_json(r)::text FROM {table} r"))
            == original
        )
        # The financial immutability trigger must survive a refused rollback.
        with pytest.raises(DBAPIError, match="append only"):
            await conn.execute(text(f"DELETE FROM {table}"))
