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


async def skip_quiet_source_emission_blocks(
    session: AsyncSession,
    *,
    netuid: int,
    block: int,
    block_hash: str,
    expected_block: int,
    expected_block_hash: str,
    runtime_code_hash: str | None,
    now: datetime,
) -> bool:
    """CAS the cursor across blocks a range plan proved quiet.

    Quiet blocks would commit no binding, payout or reset, so advancing past
    them in one step is the same durable state as advancing one at a time.
    """
    if block <= expected_block or not expected_block_hash:
        raise ValueError("quiet skip must move the cursor forward")
    result = await session.execute(
        update(SourceEmissionCollectorCursor)
        .where(
            SourceEmissionCollectorCursor.netuid == netuid,
            SourceEmissionCollectorCursor.block == expected_block,
            SourceEmissionCollectorCursor.block_hash == expected_block_hash,
        )
        .values(
            block=block,
            block_hash=block_hash,
            updated_at=now,
            runtime_code_hash=runtime_code_hash,
            last_blocked_reason=None,
        )
        .returning(SourceEmissionCollectorCursor.netuid)
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


# How many missed ranges the shadow summary keeps for diagnosis.
_SHADOW_MISS_LIMIT = 5


async def record_catch_up_range(
    session: AsyncSession,
    *,
    netuid: int,
    first_block: int,
    last_block: int,
    outcome: str,
    read: int = 0,
    skipped: int = 0,
    detail: str | None = None,
    now: datetime,
) -> None:
    """Fold one deep catch-up range into the cursor row's running summary.

    ``outcome`` is ``sparse`` (``read`` blocks read in full, ``skipped``
    committed as quiet), ``full_scan`` (the planner asked for the per-block
    scan; ``detail`` is its reason) or ``error`` (no plan; ``detail`` is the
    provider error). A fallback costs throughput, never correctness, so this
    only makes it visible.
    """
    cursor = await session.get(
        SourceEmissionCollectorCursor, netuid, with_for_update=True
    )
    if cursor is None:
        return
    summary = dict(cursor.catch_up or {})
    summary["ranges"] = int(summary.get("ranges", 0)) + 1
    summary[outcome] = int(summary.get(outcome, 0)) + 1
    summary["blocks_read"] = int(summary.get("blocks_read", 0)) + read
    summary["blocks_skipped"] = int(summary.get("blocks_skipped", 0)) + skipped
    summary["last_range"] = [first_block, last_block]
    summary["last_outcome"] = outcome
    summary["updated_at"] = now.isoformat()
    if outcome == "full_scan":
        reasons = dict(summary.get("full_scan_reasons") or {})
        reason = detail or "unknown"
        reasons[reason] = int(reasons.get(reason, 0)) + 1
        summary["full_scan_reasons"] = reasons
    if outcome == "error":
        summary["last_error"] = (detail or "")[:300]
    cursor.catch_up = summary


async def record_reveal_plan_shadow(
    session: AsyncSession,
    *,
    netuid: int,
    first_block: int,
    last_block: int,
    outcome: str,
    missed: list[int],
    calls: int,
    detail: str | None,
    now: datetime,
) -> None:
    """Fold one shadowed range into the cursor row's running summary.

    ``outcome`` is ``agreed`` (every block the full reader acted on was
    planned), ``missed`` (some were not), ``fallback`` (the planner asked for a
    full scan) or ``error``. The event planner still drives the cursor; this
    only records evidence for promoting the faster planner.
    """
    cursor = await session.get(
        SourceEmissionCollectorCursor, netuid, with_for_update=True
    )
    if cursor is None:
        return
    summary = dict(cursor.reveal_plan_shadow or {})
    summary["ranges"] = int(summary.get("ranges", 0)) + 1
    summary[outcome] = int(summary.get(outcome, 0)) + 1
    summary["blocks"] = int(summary.get("blocks", 0)) + (last_block - first_block + 1)
    summary["calls"] = int(summary.get("calls", 0)) + calls
    summary["last_range"] = [first_block, last_block]
    summary["last_outcome"] = outcome
    summary["updated_at"] = now.isoformat()
    if outcome == "missed":
        misses = list(summary.get("recent_misses") or [])
        misses.append(
            {"first_block": first_block, "last_block": last_block, "blocks": missed}
        )
        summary["recent_misses"] = misses[-_SHADOW_MISS_LIMIT:]
    if outcome == "error":
        summary["last_error"] = (detail or "")[:300]
    cursor.reveal_plan_shadow = summary
