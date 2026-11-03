"""Real PostgreSQL receipt preservation and fail-closed rollback controls."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

PARENT = "c1a72e9035bf"
HEAD = "e2b5f0a9467c"


def alembic(*args):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


@pytest.fixture(autouse=True)
async def partial_distribution_schema(engine):
    """Test this historical revision and always restore the current head.

    A refused downgrade can still commit rollback of newer empty tables before
    the financial-history guard fires. Restore the worker schema even when the
    refusal assertions fail, so the next current-head reset is valid.
    """
    assert engine.dialect.name == "postgresql"
    try:
        result = alembic("downgrade", HEAD)
        assert result.returncode == 0, result.stderr
        yield
    finally:
        result = alembic("upgrade", "head")
        assert result.returncode == 0, result.stderr


async def indexes(engine):
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT indexname,indexdef FROM pg_indexes WHERE "
                        "tablename='treasury_verified_receipts' ORDER BY indexname"
                    )
                )
            ).all()
        )


async def test_empty_partial_receipt_index_roundtrip(engine):
    assert engine.dialect.name == "postgresql"
    before = await indexes(engine)
    try:
        result = alembic("downgrade", PARENT)
        assert result.returncode == 0, result.stderr
        old = dict(await indexes(engine))
        assert "UNIQUE INDEX" in old["treasury_verified_distribution_once"]
        result = alembic("upgrade", HEAD)
        assert result.returncode == 0, result.stderr
        assert await indexes(engine) == before
    finally:
        result = alembic("upgrade", "head")
        assert result.returncode == 0, result.stderr


async def test_partial_receipt_history_refuses_downgrade_without_mutation(engine):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO treasury_settings_revisions "
                "(revision,parent_revision,settings,checksum,reason,actor) "
                "VALUES (1,0,'{}',repeat('a',64),'retained policy','test-actor')"
            )
        )
        for block, amount in [(120, 15), (121, 25)]:
            await conn.execute(
                text(
                    "INSERT INTO treasury_verified_receipts "
                    "(receipt_id,request_digest,stage,policy_digest,collector_policy_digest,"
                    "bucket_id,block_hash,reason,actor,epoch_index,source_block,amount_atomic,"
                    "settings_revision,extrinsic_index,event_index,proof,published) "
                    "VALUES (:id,:id,'service_distribution','policy','collector',"
                    "'gamma',:id,'retained receipt','actor',1,110,:amount,"
                    "1,0,0,'{}',false)"
                ),
                {"id": str(block), "amount": amount},
            )
    async with engine.connect() as conn:
        original = list(
            (
                await conn.execute(
                    text(
                        "SELECT row_to_json(r)::text FROM "
                        "treasury_verified_receipts r ORDER BY receipt_id"
                    )
                )
            ).scalars()
        )
    before = await indexes(engine)
    result = alembic("downgrade", PARENT)
    assert result.returncode != 0
    assert "Retained distributions prevent single-leg downgrade" in result.stderr
    assert await indexes(engine) == before
    async with engine.connect() as conn:
        assert (
            await conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        )
        assert (
            list(
                (
                    await conn.execute(
                        text(
                            "SELECT row_to_json(r)::text FROM "
                            "treasury_verified_receipts r ORDER BY receipt_id"
                        )
                    )
                ).scalars()
            )
            == original
        )
