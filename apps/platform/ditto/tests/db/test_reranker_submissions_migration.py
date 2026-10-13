"""Real-PG upgrade/downgrade of the reranker submissions table (SN-207)."""

import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

PARENT = "7d3ca1e6a290"
REVISION = "3e8b5c1d9a47"


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


@pytest.fixture(autouse=True)
async def restore_head(engine: AsyncEngine):
    assert engine.dialect.name == "postgresql"
    try:
        yield
    finally:
        alembic("upgrade", "head")


async def _table_exists(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        return bool(
            (
                await conn.execute(
                    text(
                        "SELECT to_regclass('public.reranker_submissions') IS NOT NULL"
                    )
                )
            ).scalar_one()
        )


async def _shape(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT conname::text, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'reranker_submissions'::regclass UNION ALL "
                "SELECT indexname::text, indexdef FROM pg_indexes "
                "WHERE tablename = 'reranker_submissions' ORDER BY 1"
            )
        )
        return [(name, definition) for name, definition in rows.all()]


async def test_downgrade_drops_and_upgrade_restores_the_table(
    engine: AsyncEngine,
) -> None:
    assert await _table_exists(engine)
    before = await _shape(engine)

    alembic("downgrade", PARENT)
    assert not await _table_exists(engine)

    alembic("upgrade", REVISION)
    assert await _table_exists(engine)
    assert await _shape(engine) == before

    names = {name for name, _ in before}
    assert {
        "reranker_submissions_pkey",
        "reranker_submissions_artifact_sha256_key",
        "ck_reranker_submissions_status",
        "ck_reranker_submissions_digests",
        "ck_reranker_submissions_size",
        "ck_reranker_submissions_base_model",
        "ck_reranker_submissions_seed",
        "reranker_submissions_status_created_idx",
        "reranker_submissions_hotkey_created_idx",
        "reranker_submissions_coldkey_created_idx",
    } <= names


async def test_status_defaults_to_accepted_and_agents_are_untouched(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO reranker_submissions (id, miner_hotkey, upload_sha256, "
                "artifact_sha256, contract_sha256, size_bytes, base_model, "
                "base_model_license) VALUES (gen_random_uuid(), '5H', "
                "repeat('a', 64), repeat('b', 64), repeat('c', 64), 1, 'none', 'mit')"
            )
        )
        status = (
            await conn.execute(text("SELECT status FROM reranker_submissions"))
        ).scalar_one()
        agents = (await conn.execute(text("SELECT count(*) FROM agents"))).scalar_one()
    assert status == "accepted"
    assert agents == 0
