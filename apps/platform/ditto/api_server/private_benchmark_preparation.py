"""Resolve exact private lease bytes without inference in a lease transaction."""

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.datapipeline import DatasetGenerator
from ditto.api_server.errors import ApiServerConfigError
from ditto.db.models import PrivateBenchmarkPreparation
from ditto.db.queries.private_benchmark_datasets import (
    PrivateDatasetIdentity,
    find_private_dataset,
)
from ditto.db.queries.private_benchmark_preparations import request_private_preparation


@dataclass(frozen=True)
class PrivatePreparationConfig:
    profile_sha256: str | None = None
    max_pending: int = 8
    max_daily: int = 8


def check_private_preparation_config(config: PrivatePreparationConfig) -> None:
    if (
        config.profile_sha256 is not None
        and not re.fullmatch(r"[0-9a-f]{64}", config.profile_sha256)
    ) or not (1 <= config.max_pending <= 1024 and 1 <= config.max_daily <= 1024):
        raise ApiServerConfigError("private preparation configuration invalid")


def parse_private_preparation_config() -> PrivatePreparationConfig:
    try:
        config = PrivatePreparationConfig(
            profile_sha256=os.environ.get("DITTO_PRIVATE_DATASET_PROFILE_SHA256")
            or None,
            max_pending=int(os.environ.get("DITTO_PRIVATE_DATASET_MAX_PENDING", "8")),
            max_daily=int(os.environ.get("DITTO_PRIVATE_DATASET_MAX_DAILY", "8")),
        )
    except ValueError:
        raise ApiServerConfigError("private preparation limits invalid") from None
    check_private_preparation_config(config)
    return config


def private_identity(seed: int, run_size: str, profile: str) -> PrivateDatasetIdentity:
    # Deliberately no agent or validator identity: CRN comparisons using the
    # same seed/profile must use one object, not independently paraphrased sides.
    return PrivateDatasetIdentity("bench-v13", seed, run_size, profile)


async def resolve_private_dataset(
    sessions: async_sessionmaker[AsyncSession],
    config: PrivatePreparationConfig,
    *,
    seed: int,
    run_size: str,
    now: datetime,
) -> str:
    """Return a committed pin, or commit bounded preparation and decline a lease.

    This intentionally uses an independent short transaction: a 503 rolls back
    the caller's lease, not its durable preparation. No foreign key or row lock
    links the preparation table to the caller's lease transaction.
    """
    check_private_preparation_config(config)
    if config.profile_sha256 is None:
        raise HTTPException(503, "private V13 production is not configured")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("private preparation requires an aware clock")
    identity = private_identity(seed, run_size, config.profile_sha256)
    identity_hash = identity.digest()
    status = "pending"
    async with sessions() as session, session.begin():
        existing = await find_private_dataset(session, identity=identity)
        if existing is not None:
            return existing.dataset_sha256
        # Serialize only budget admission, never inference or a live ticket.
        # Count across profiles so changing a profile cannot reset the budget.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended("private-dataset-budget", 0)
                )
            )
        )
        row = await session.scalar(
            select(PrivateBenchmarkPreparation).where(
                PrivateBenchmarkPreparation.identity_sha256 == identity_hash
            )
        )
        if row is None:
            pending = await session.scalar(
                select(func.count())
                .select_from(PrivateBenchmarkPreparation)
                .where(PrivateBenchmarkPreparation.state.in_(("pending", "running")))
            )
            daily = await session.scalar(
                select(func.count())
                .select_from(PrivateBenchmarkPreparation)
                .where(
                    PrivateBenchmarkPreparation.created_at >= now - timedelta(days=1)
                )
            )
            if (pending or 0) >= config.max_pending or (daily or 0) >= config.max_daily:
                raise HTTPException(503, "private dataset preparation budget exhausted")
            await request_private_preparation(session, identity=identity)
        else:
            status = row.state
    detail = (
        "private dataset preparation failed; operator review required"
        if status == "failed"
        else "private dataset preparation pending"
    )
    raise HTTPException(
        503, detail, headers={"Retry-After": "60", "Cache-Control": "no-store"}
    )


async def lease_dataset_sha(
    request: Request, generator: DatasetGenerator, *, seed: int, bench_version: int
) -> str:
    if bench_version != 13:
        return await generator.generate(seed, bench_version=bench_version)
    if generator.run_size is None:
        raise HTTPException(503, "private dataset run profile is unavailable")
    config = request.app.state.config.private_preparation
    sessions = getattr(request.app.state, "private_preparation_sessions", None)
    if config.profile_sha256 is None or sessions is None:
        raise HTTPException(503, "private V13 production is not configured")
    return await resolve_private_dataset(
        sessions,
        config,
        seed=seed,
        run_size=generator.run_size,
        now=datetime.now(UTC),
    )
