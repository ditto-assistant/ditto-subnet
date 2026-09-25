"""Real-Postgres immutable private artifact pinning, not qualification."""

import asyncio
import hashlib
import json
import traceback
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from ditto.db.models import PrivateBenchmarkDataset
from ditto.db.queries.private_benchmark_datasets import (
    MAX_RECEIPT_BYTES,
    PrivateDatasetError,
    PrivateDatasetIdentity,
    find_private_dataset,
    pin_private_dataset,
)


def identity():
    return PrivateDatasetIdentity(str(uuid4()), 123, "full", "a" * 64)


def candidate(key, text="private", salt=1):
    base = json.dumps(
        {
            "seed": key.seed,
            "bench_version": 13,
            "surface_salt": salt,
            "prompt": "public",
        }
    ).encode()
    dataset = json.dumps(
        {"seed": key.seed, "bench_version": 13, "surface_salt": salt, "prompt": text}
    ).encode()
    receipt = json.dumps(
        {
            "schema": "private-surface-validation-v1",
            "accepted": True,
            "base_sha256": hashlib.sha256(base).hexdigest(),
            "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
            "transform_profile_sha256": key.transform_profile_sha256,
        }
    ).encode()
    return {
        "base_bytes": base,
        "dataset_bytes": dataset,
        "validation_receipt_bytes": receipt,
    }


async def test_driver_failure_cannot_leak_private_input_in_traceback():
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("SECRET failing row and bind bytes")
    key = identity()
    try:
        await pin_private_dataset(session, identity=key, **candidate(key))
    except PrivateDatasetError:
        assert "SECRET" not in traceback.format_exc()
    else:
        pytest.fail("storage failure was accepted")


async def test_retry_and_restart_reuse_exact_bytes(session_maker):
    key = identity()
    async with session_maker() as session, session.begin():
        first = await pin_private_dataset(session, identity=key, **candidate(key))
    async with session_maker() as session, session.begin():
        retry = await pin_private_dataset(
            session, identity=key, **candidate(key, "different", 2)
        )
        assert retry == first
    async with session_maker() as session:
        assert await find_private_dataset(session, identity=key) == first
        assert "private" not in repr(first).split("dataset_sha256")[1]


async def test_full_profile_receipt_capacity_is_bounded(session_maker):
    key = identity()
    values = candidate(key)
    receipt = json.loads(values["validation_receipt_bytes"])
    receipt["surface_provenance"] = "x" * (6 << 20)
    values["validation_receipt_bytes"] = json.dumps(receipt).encode()
    async with session_maker() as session, session.begin():
        result = await pin_private_dataset(session, identity=key, **values)
    async with session_maker() as session:
        assert await find_private_dataset(session, identity=key) == result
    receipt["surface_provenance"] = "x" * MAX_RECEIPT_BYTES
    values["validation_receipt_bytes"] = json.dumps(receipt).encode()
    async with session_maker() as session, session.begin():
        with pytest.raises(PrivateDatasetError):
            await pin_private_dataset(session, identity=key, **values)


async def test_concurrent_candidates_have_one_winner(session_maker):
    key = identity()

    async def write(n):
        async with session_maker() as session, session.begin():
            return await pin_private_dataset(
                session, identity=key, **candidate(key, f"candidate-{n}", n + 1)
            )

    results = await asyncio.gather(*(write(n) for n in range(4)))
    assert all(result == results[0] for result in results)


async def test_rollback_does_not_pin_candidate(session_maker):
    key = identity()
    async with session_maker() as session:
        await pin_private_dataset(session, identity=key, **candidate(key, "aborted"))
        await session.rollback()
    async with session_maker() as session, session.begin():
        assert await find_private_dataset(session, identity=key) is None
        result = await pin_private_dataset(
            session, identity=key, **candidate(key, "committed")
        )
        assert b"committed" in result.dataset_bytes


async def test_database_rejects_update_delete_and_digest_mismatch(session_maker):
    key = identity()
    async with session_maker() as session, session.begin():
        result = await pin_private_dataset(session, identity=key, **candidate(key))
    for statement in (
        update(PrivateBenchmarkDataset)
        .where(PrivateBenchmarkDataset.dataset_id == result.dataset_id)
        .values(dataset_sha256="b" * 64),
        delete(PrivateBenchmarkDataset).where(
            PrivateBenchmarkDataset.dataset_id == result.dataset_id
        ),
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            async with session_maker() as session, session.begin():
                await session.execute(statement)
    with pytest.raises(DBAPIError):
        async with session_maker() as session, session.begin():
            session.add(
                PrivateBenchmarkDataset(
                    dataset_id=uuid4(),
                    identity_sha256="b" * 64,
                    scope="invalid",
                    seed=123,
                    bench_version=13,
                    run_size="full",
                    transform_profile_sha256="a" * 64,
                    validation_receipt_sha256="a" * 64,
                    base_sha256="a" * 64,
                    dataset_sha256="a" * 64,
                    base_bytes=b"{}",
                    dataset_bytes=b"{}",
                    validation_receipt_bytes=b"{}",
                )
            )


@pytest.mark.parametrize(
    "change",
    [
        {"base_bytes": b"{}"},
        {"dataset_bytes": b"{}"},
        {"validation_receipt_bytes": b'{"accepted":false}'},
        {"dataset_bytes": b'{"seed":123,"seed":123}'},
    ],
)
async def test_bad_candidate_never_pins(session_maker, change):
    key = identity()
    values = candidate(key)
    values.update(change)
    async with session_maker() as session, session.begin():
        with pytest.raises(PrivateDatasetError):
            await pin_private_dataset(session, identity=key, **values)
        assert await find_private_dataset(session, identity=key) is None


async def test_scope_and_profile_are_separate_identities(session_maker):
    key = identity()
    keys = [
        key,
        replace(key, scope="another"),
        replace(key, transform_profile_sha256="b" * 64),
    ]
    async with session_maker() as session, session.begin():
        rows = [
            await pin_private_dataset(session, identity=k, **candidate(k)) for k in keys
        ]
        assert len({row.dataset_id for row in rows}) == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed": True},
        {"seed": -1},
        {"seed": 2**63},
        {"run_size": "unknown"},
        {"scope": ""},
        {"scope": "x\n"},
        {"transform_profile_sha256": "X" * 64},
        {"bench_version": 12},
    ],
)
def test_identity_fail_closed(kwargs):
    with pytest.raises(PrivateDatasetError):
        replace(identity(), **kwargs).digest()
