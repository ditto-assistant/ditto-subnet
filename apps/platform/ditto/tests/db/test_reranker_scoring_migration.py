"""Real-PG upgrade/downgrade of the reranker scoring lease and crown history."""

import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

PARENT = "3e8b5c1d9a47"
REVISION = "f8af34f45dea"


def alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=check,
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


async def _scalar(engine: AsyncEngine, sql: str):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


async def _shape(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT conname::text, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid IN ('reranker_submissions'::regclass, "
                "'reranker_champions'::regclass) UNION ALL "
                "SELECT indexname::text, indexdef FROM pg_indexes WHERE tablename "
                "IN ('reranker_submissions', 'reranker_champions') UNION ALL "
                "SELECT column_name::text, data_type::text FROM "
                "information_schema.columns WHERE table_name = "
                "'reranker_submissions' ORDER BY 1, 2"
            )
        )
        return [(name, definition) for name, definition in rows.all()]


_INSERT = (
    "INSERT INTO reranker_submissions (id, miner_hotkey, upload_sha256, "
    "artifact_sha256, contract_sha256, size_bytes, base_model, "
    "base_model_license{extra_cols}) VALUES ({id}, '5H', repeat('a', 64), "
    "repeat({digest}, 64), repeat('c', 64), 1, 'none', 'mit'{extra_vals})"
)


async def test_downgrade_and_upgrade_round_trip(engine: AsyncEngine) -> None:
    before = await _shape(engine)
    names = {name for name, _ in before}
    assert {
        "reranker_champions_pkey",
        "reranker_champions_one_current_idx",
        "ck_reranker_submissions_scoring_lease",
        "ck_reranker_submissions_seed_block",
        "scoring_token",
        "seed_block",
    } <= names

    # An in-flight lease goes back to the queue on downgrade.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                _INSERT.format(
                    id="'00000000-0000-4000-8000-000000000001'",
                    digest="'b'",
                    extra_cols=", status, scoring_token, scoring_lease_expires_at",
                    extra_vals=", 'scoring', gen_random_uuid(), now()",
                )
            )
        )
    alembic("downgrade", PARENT)
    assert not await _scalar(
        engine, "SELECT to_regclass('public.reranker_champions') IS NOT NULL"
    )
    assert await _scalar(engine, "SELECT status FROM reranker_submissions") == (
        "accepted"
    )
    alembic("upgrade", REVISION)
    assert await _shape(engine) == before


async def test_downgrade_refuses_to_drop_crown_history(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                _INSERT.format(
                    id="'00000000-0000-4000-8000-000000000002'",
                    digest="'e'",
                    extra_cols="",
                    extra_vals="",
                )
            )
        )
        await conn.execute(
            text(
                "INSERT INTO reranker_champions (submission_id, contract_sha256, "
                "pools_sha256, comparison) VALUES "
                "('00000000-0000-4000-8000-000000000002', repeat('c', 64), "
                "repeat('d', 64), '{}')"
            )
        )
    result = alembic("downgrade", PARENT, check=False)
    assert result.returncode != 0
    assert "crown history" in result.stderr
    assert await _scalar(engine, "SELECT count(*) FROM reranker_champions") == 1
