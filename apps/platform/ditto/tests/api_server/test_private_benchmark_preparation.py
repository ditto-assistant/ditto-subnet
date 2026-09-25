"""Private preparation survives lease rollback without inference or unbounded work."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from ditto.api_server.private_benchmark_preparation import (
    PrivatePreparationConfig,
    lease_dataset_sha,
    private_identity,
    resolve_private_dataset,
)
from ditto.db.models import PrivateBenchmarkPreparation
from ditto.db.queries.private_benchmark_preparations import (
    claim_private_preparation,
    fail_private_preparation,
    finish_private_preparation,
)
from ditto.tests.db.test_private_benchmark_datasets import candidate


async def resolve(sessions, config, seed=42):
    return await resolve_private_dataset(
        sessions, config, seed=seed, run_size="full", now=datetime.now(UTC)
    )


async def test_private_disabled_never_reserves(session_maker):
    with pytest.raises(HTTPException, match="not configured"):
        await resolve(session_maker, PrivatePreparationConfig())
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(PrivateBenchmarkPreparation)
            )
            == 0
        )


async def test_pending_commits_before_declining_lease(session_maker):
    config = PrivatePreparationConfig("a" * 64)
    async with session_maker() as outer, outer.begin():
        with pytest.raises(HTTPException, match="pending"):
            await resolve(session_maker, config)
        await outer.rollback()
    async with session_maker() as session:
        row = await session.scalar(select(PrivateBenchmarkPreparation))
        assert row.state == "pending" and row.attempts == 0


async def test_ready_reuses_pin_and_preserves_crn_identity(session_maker):
    config = PrivatePreparationConfig("a" * 64)
    with pytest.raises(HTTPException):
        await resolve(session_maker, config)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        claim = await claim_private_preparation(
            session, transform_profile_sha256=config.profile_sha256, now=now
        )
    key = private_identity(42, "full", config.profile_sha256)
    async with session_maker() as session, session.begin():
        result = await finish_private_preparation(
            session,
            claim=claim,
            now=now,
            **candidate(key, salt=int.from_bytes(claim.surface_salt, "big")),
        )
    assert await resolve(session_maker, config) == result.dataset_sha256
    assert await resolve(session_maker, config) == result.dataset_sha256
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(PrivateBenchmarkPreparation)
            )
            == 1
        )


async def test_concurrent_budget_is_atomic_across_profiles(session_maker):
    configs = [PrivatePreparationConfig(digest * 64, 1, 1) for digest in ("a", "b")]
    results = await asyncio.gather(
        *(resolve(session_maker, config) for config in configs), return_exceptions=True
    )
    assert all(isinstance(result, HTTPException) for result in results)
    assert sorted(result.detail for result in results) == [
        "private dataset preparation budget exhausted",
        "private dataset preparation pending",
    ]
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(PrivateBenchmarkPreparation)
            )
            == 1
        )


async def test_failed_work_is_not_requeued_or_refunded(session_maker):
    config = PrivatePreparationConfig("a" * 64, 1, 1)
    with pytest.raises(HTTPException, match="pending"):
        await resolve(session_maker, config)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        claim = await claim_private_preparation(
            session, transform_profile_sha256=config.profile_sha256, now=now
        )
        await fail_private_preparation(
            session, claim=claim, now=now, code="producer_rejected"
        )
    with pytest.raises(HTTPException, match="operator review"):
        await resolve(session_maker, config)
    with pytest.raises(HTTPException, match="budget exhausted"):
        await resolve(session_maker, config, seed=43)


@pytest.mark.parametrize("version", [12, 13])
async def test_production_versions_use_public_generator_without_private_config(version):
    generator = AsyncMock()
    generator.generate.return_value = "a" * 64
    assert (
        await lease_dataset_sha(None, generator, seed=42, bench_version=version)
        == "a" * 64
    )
    generator.generate.assert_awaited_once_with(42, bench_version=version)
