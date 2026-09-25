"""Real PostgreSQL preparation fencing and entropy retention."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from ditto.db.models import PrivateBenchmarkPreparation
from ditto.db.queries.private_benchmark_datasets import (
    PrivateDatasetError,
    find_private_dataset,
)
from ditto.db.queries.private_benchmark_preparations import (
    CLAIM_TTL,
    claim_private_preparation,
    fail_private_preparation,
    finish_private_preparation,
    request_private_preparation,
)
from ditto.tests.db.test_private_benchmark_datasets import candidate, identity


async def reserve(maker, key):
    async with maker() as session, session.begin():
        return await request_private_preparation(session, identity=key)


async def claim(maker, key, now):
    async with maker() as session, session.begin():
        return await claim_private_preparation(
            session, transform_profile_sha256=key.transform_profile_sha256, now=now
        )


async def test_concurrent_reservations_and_claims_have_one_owner(session_maker):
    key = identity()
    ids = await asyncio.gather(*(reserve(session_maker, key) for _ in range(5)))
    assert len(set(ids)) == 1
    now = datetime.now(UTC)
    claims = await asyncio.gather(*(claim(session_maker, key, now) for _ in range(3)))
    owned = [value for value in claims if value is not None]
    assert len(owned) == 1
    assert owned[0].preparation_id == ids[0]
    assert owned[0].surface_salt != bytes(8)
    assert "surface_salt" not in repr(owned[0])


async def test_crash_recovery_keeps_entropy_and_fences_stale_writer(session_maker):
    key = identity()
    await reserve(session_maker, key)
    now = datetime.now(UTC)
    first = await claim(session_maker, key, now)
    later = now + CLAIM_TTL + timedelta(seconds=1)
    second = await claim(session_maker, key, later)
    assert first.token != second.token
    assert first.surface_salt == second.surface_salt
    values = candidate(key, salt=int.from_bytes(second.surface_salt, "big"))
    async with session_maker() as session, session.begin():
        with pytest.raises(PrivateDatasetError, match="stale"):
            await finish_private_preparation(session, claim=first, now=later, **values)
    async with session_maker() as session, session.begin():
        result = await finish_private_preparation(
            session, claim=second, now=later, **values
        )
    assert await claim(session_maker, key, later) is None
    async with session_maker() as session:
        assert await find_private_dataset(session, identity=key) == result
        row = await session.get(PrivateBenchmarkPreparation, first.preparation_id)
        assert row.state == "ready" and row.dataset_id == result.dataset_id


async def test_expiry_has_bounded_attempt_budget(session_maker):
    key = identity()
    prep = await reserve(session_maker, key)
    now = datetime.now(UTC)
    for attempt in range(3):
        current = await claim(session_maker, key, now)
        assert current.attempt == attempt + 1
        now += CLAIM_TTL + timedelta(seconds=1)
    assert await claim(session_maker, key, now) is None
    async with session_maker() as session:
        row = await session.get(PrivateBenchmarkPreparation, prep)
        assert row.state == "failed" and row.failure_code == "attempts_exhausted"


async def test_rejection_is_not_automatically_retried(session_maker):
    key = identity()
    await reserve(session_maker, key)
    now = datetime.now(UTC)
    current = await claim(session_maker, key, now)
    async with session_maker() as session, session.begin():
        with pytest.raises(PrivateDatasetError, match="code"):
            await fail_private_preparation(
                session, claim=current, now=now, code="RAW SECRET"
            )
        await fail_private_preparation(
            session, claim=current, now=now, code="producer_rejected"
        )
    assert await claim(session_maker, key, now + CLAIM_TTL) is None


async def test_completion_rollback_never_leaves_half_ready_pin(session_maker):
    key = identity()
    prep = await reserve(session_maker, key)
    now = datetime.now(UTC)
    current = await claim(session_maker, key, now)
    values = candidate(key, salt=int.from_bytes(current.surface_salt, "big"))
    async with session_maker() as session:
        await finish_private_preparation(session, claim=current, now=now, **values)
        await session.rollback()
    async with session_maker() as session:
        assert await find_private_dataset(session, identity=key) is None
        row = await session.get(PrivateBenchmarkPreparation, prep)
        assert row.state == "running"


async def test_wrong_entropy_and_profile_never_publish(session_maker):
    key = identity()
    await reserve(session_maker, key)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        assert (
            await claim_private_preparation(
                session, transform_profile_sha256="b" * 64, now=now
            )
            is None
        )
    current = await claim(session_maker, key, now)
    wrong_salt = int.from_bytes(current.surface_salt, "big") ^ 1
    async with session_maker() as session, session.begin():
        with pytest.raises(PrivateDatasetError, match="entropy"):
            await finish_private_preparation(
                session, claim=current, now=now, **candidate(key, salt=wrong_salt)
            )
        assert await find_private_dataset(session, identity=key) is None


async def test_entropy_is_immutable_in_database(session_maker):
    key = identity()
    prep = await reserve(session_maker, key)
    with pytest.raises(DBAPIError, match="immutable"):
        async with session_maker() as session, session.begin():
            await session.execute(
                update(PrivateBenchmarkPreparation)
                .where(PrivateBenchmarkPreparation.preparation_id == prep)
                .values(surface_salt=b"x" * 8)
            )


async def test_rollback_reservation_does_not_create_work(session_maker):
    key = identity()
    async with session_maker() as session:
        await request_private_preparation(session, identity=key)
        await session.rollback()
    async with session_maker() as session:
        assert (
            await session.scalar(select(PrivateBenchmarkPreparation.preparation_id))
            is None
        )
