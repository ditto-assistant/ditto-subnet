"""Transactional finalized-block cursor and current vector provenance."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import (
    SourceEmissionCollectorCursor,
    SourceEmissionPayout,
    SourceEmissionVectorBinding,
)


async def get_source_emission_cursor(
    session: AsyncSession,
    *,
    netuid: int,
) -> SourceEmissionCollectorCursor | None:
    return await session.get(SourceEmissionCollectorCursor, netuid)


async def advance_source_emission_cursor(
    session: AsyncSession,
    *,
    netuid: int,
    block: int,
    block_hash: str,
    expected_block: int | None,
    expected_block_hash: str | None,
    now: datetime,
) -> bool:
    """CAS cursor in the same transaction as bindings and payout evidence.

    False means the caller must roll back its entire block transaction.
    Initialization must explicitly choose a finalized starting point; all
    subsequent advances must be contiguous and parent-hash checked by caller.
    """
    if expected_block is None:
        if expected_block_hash is not None:
            raise ValueError("initial cursor cannot have an expected hash")
        result = await session.execute(
            insert(SourceEmissionCollectorCursor)
            .values(
                netuid=netuid,
                block=block,
                block_hash=block_hash,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["netuid"])
            .returning(
                SourceEmissionCollectorCursor.netuid,
            )
        )
    else:
        if block != expected_block + 1 or not expected_block_hash:
            raise ValueError("cursor must advance exactly one finalized block")
        result = await session.execute(
            update(SourceEmissionCollectorCursor)
            .where(
                SourceEmissionCollectorCursor.netuid == netuid,
                SourceEmissionCollectorCursor.block == expected_block,
                SourceEmissionCollectorCursor.block_hash == expected_block_hash,
            )
            .values(block=block, block_hash=block_hash, updated_at=now)
            .returning(
                SourceEmissionCollectorCursor.netuid,
            )
        )
    return result.scalar_one_or_none() is not None


async def list_source_emission_vector_bindings(
    session: AsyncSession,
    *,
    netuid: int,
) -> list[SourceEmissionVectorBinding]:
    return list(
        await session.scalars(
            select(SourceEmissionVectorBinding).where(
                SourceEmissionVectorBinding.netuid == netuid,
            )
        )
    )


async def upsert_source_emission_vector_binding(
    session: AsyncSession,
    *,
    netuid: int,
    validator_hotkey: str,
    receipt_digest: str | None,
    block: int,
    reveal_block_hash: str,
    vector_digest: str | None,
    evidence: dict,
) -> None:
    """Record every vector write; an unknown write explicitly clears attribution."""
    values = {
        "netuid": netuid,
        "validator_hotkey": validator_hotkey,
        "receipt_digest": receipt_digest,
        "block": block,
        "reveal_block_hash": reveal_block_hash,
        "vector_digest": vector_digest,
        "evidence": evidence,
    }
    statement = insert(SourceEmissionVectorBinding).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["netuid", "validator_hotkey"],
            set_={
                k: v
                for k, v in values.items()
                if k not in {"netuid", "validator_hotkey"}
            },
            where=SourceEmissionVectorBinding.block <= block,
        )
    )


async def record_source_emission_payout(
    session: AsyncSession,
    *,
    netuid: int,
    block: int,
    block_hash: str,
    proof: dict,
    now: datetime,
) -> bool:
    result = await session.execute(
        insert(SourceEmissionPayout)
        .values(
            netuid=netuid,
            block=block,
            block_hash=block_hash,
            proof=proof,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["netuid", "block_hash"])
        .returning(
            SourceEmissionPayout.netuid,
        )
    )
    return result.scalar_one_or_none() is not None
