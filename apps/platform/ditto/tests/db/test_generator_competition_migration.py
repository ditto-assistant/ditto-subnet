"""The generator competition tables round-trip and enforce their invariants."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "5f2c8e1a9d47"


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _insert_submission(engine: AsyncEngine, **overrides: object) -> str:
    values: dict[str, object] = {
        "submission_id": str(uuid4()),
        "sha": "a" * 64,
        "status": "active",
        "bytes": b"\x1f\x8b",
        "size": 2,
        "lease_id": None,
    }
    values.update(overrides)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO generator_submissions (submission_id, miner_hotkey, "
                "owner_key, source_sha256, tree_sha256, source_bytes, size_bytes, "
                "module, go_version, file_count, status, next_run_at, lease_id) "
                "VALUES (CAST(:submission_id AS uuid), 'hk', 'hotkey:hk', :sha, "
                "repeat('b', 64), :bytes, :size, 'example.com/g', '1.23', 2, "
                ":status, now(), CAST(:lease_id AS uuid))"
            ),
            values,
        )
    return str(values["submission_id"])


async def _clear(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM generator_submissions"))
        await connection.execute(text("DELETE FROM generator_probe_epochs"))


async def test_generator_competition_round_trip(engine: AsyncEngine) -> None:
    try:
        await _clear(engine)
        submission = await _insert_submission(engine)
        with pytest.raises(IntegrityError):  # duplicate identity
            await _insert_submission(engine)
        with pytest.raises(IntegrityError):  # size must match the bytes
            await _insert_submission(engine, sha="b" * 64, size=99)
        with pytest.raises(IntegrityError):  # a lease is all or nothing
            await _insert_submission(engine, sha="c" * 64, lease_id=str(uuid4()))
        with pytest.raises(IntegrityError):  # unknown status
            await _insert_submission(engine, sha="d" * 64, status="limbo")

        async with engine.begin() as connection:
            epoch = await connection.scalar(
                text(
                    "INSERT INTO generator_probe_epochs (seed, reason, actor) "
                    "VALUES (repeat('e', 64), 'initial', 'test') RETURNING epoch"
                )
            )
        with pytest.raises(IntegrityError):  # one current probe epoch
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO generator_probe_epochs (seed, reason, actor) "
                        "VALUES (repeat('f', 64), 'second', 'test')"
                    )
                )
        run_sql = (
            "INSERT INTO generator_runs (run_id, submission_id, attempt, seeds, "
            "probe_epoch, cases_per_seed, panel, settings_revision, status, score, "
            "finished_at) VALUES (gen_random_uuid(), CAST(:sub AS uuid), 1, '[]', "
            ":epoch, 16, '[]', 1, :status, :score, :finished)"
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(run_sql),
                {
                    "sub": submission,
                    "epoch": epoch,
                    "status": "leased",
                    "score": None,
                    "finished": None,
                },
            )
        with pytest.raises(IntegrityError):  # a scored run carries a score
            async with engine.begin() as connection:
                await connection.execute(
                    text(run_sql),
                    {
                        "sub": submission,
                        "epoch": epoch,
                        "status": "scored",
                        "score": None,
                        "finished": datetime.now(UTC),
                    },
                )
        await _clear(engine)

        _alembic("downgrade", _PARENT)
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT to_regclass('public.generator_submissions')")
            )
        assert exists is None
        _alembic("upgrade", "head")
        await _insert_submission(engine, sha="e" * 64)
    finally:
        await _clear(engine)
        _alembic("upgrade", "head")


async def test_generator_rounds_enforce_their_invariants(engine: AsyncEngine) -> None:
    round_sql = (
        "INSERT INTO generator_rounds (round_index, status, epochs_per_round, "
        "secret, commitment, committed_at, committed_epoch_index, "
        "commitment_publication, seed, void_reason) VALUES (:idx, :status, 4, "
        "decode(repeat('ab', 32), 'hex'), repeat('c', 64), now(), 1, :pub, "
        ":seed, :void)"
    )

    async def insert(**overrides: object) -> None:
        values: dict[str, object] = {
            "idx": 1,
            "status": "committed",
            "pub": "pending",
            "seed": None,
            "void": None,
        }
        values.update(overrides)
        async with engine.begin() as connection:
            await connection.execute(text(round_sql), values)

    try:
        await insert()
        # A round voided before it opened has no seed; that is allowed.
        await insert(idx=2, status="void", void="never opened")
        await insert(idx=3, status="generating", seed="d" * 64)
        for bad in (
            {"idx": 4, "status": "committed", "seed": "d" * 64},  # seed leaks early
            {"idx": 5, "status": "running"},  # opened without a seed
            {"idx": 6, "status": "void"},  # void needs a reason
            {"idx": 7, "pub": "maybe"},  # unknown publication state
            {"idx": 8, "status": "limbo"},
        ):
            with pytest.raises(IntegrityError):
                await insert(**bad)
        with pytest.raises(IntegrityError):  # the digest must match the bytes
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE generator_rounds SET dataset_bytes = 'x', "
                        "dataset_sha256 = repeat('0', 64) WHERE round_index = 3"
                    )
                )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE generator_rounds SET dataset_bytes = 'x', "
                    "dataset_sha256 = encode(sha256('x'), 'hex') "
                    "WHERE round_index = 3"
                )
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM generator_rounds"))
