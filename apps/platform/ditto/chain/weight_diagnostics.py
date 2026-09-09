"""Bounded public chain evidence for vTrust; never a weight-setting path."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainWeightsSnapshot

if TYPE_CHECKING:
    from ditto.chain.client import ChainClient

logger = logging.getLogger(__name__)

# Drand Quicknet constants from bittensor-drand 2.0.0 src/constants.rs. A
# commitment's ``reveal_round`` is the pulse the committer encrypted for, so
# its wall-clock time reveals which block the committer was aiming at.
DRAND_GENESIS_TIME = 1_692_803_367
DRAND_PERIOD_SECONDS = 3
# The nominal block time TurboBT passes to drand when it converts blocks to a
# round; the same figure turns the round back into a block here.
BLOCK_SECONDS = 12
# drand v2 targets the first legal reveal block plus this many blocks.
SECURITY_BLOCK_OFFSET = 3
# Subtensor's owner-set tempo ceiling, also the BlocksSinceLastStep safety net.
MAX_TEMPO = 50_400
# Concurrent per-commit timestamp reads against one substrate connection.
_TIMESTAMP_READ_CONCURRENCY = 8


@dataclass(frozen=True)
class PendingWeightCommit:
    hotkey: str
    epoch: int
    commit_block: int
    reveal_round: int
    # Seconds; ``None`` when the commit block's timestamp state was unreadable.
    commit_block_timestamp: int | None = None

    @property
    def implied_reveal_block(self) -> int | None:
        """The block the committer's drand round points at, to about one block.

        drand derives ``reveal_round`` from wall-clock time at encryption plus
        ``blocks_until_ingest * block_time``; running that backwards from the
        commit block's on-chain timestamp recovers the targeted block. The
        commit block is the head drand saw plus its inclusion delay, and the
        round is floored to a three-second pulse, so the result carries about
        one block of noise. That is far below the hundreds of blocks that
        separate a legacy same-epoch reveal from a stateful boundary + 3 one.
        """
        if self.commit_block_timestamp is None:
            return None
        round_time = DRAND_GENESIS_TIME + self.reveal_round * DRAND_PERIOD_SECONDS
        blocks = round((round_time - self.commit_block_timestamp) / BLOCK_SECONDS)
        return max(0, self.commit_block + blocks)


@dataclass(frozen=True)
class WeightDiagnostics:
    snapshot: ChainWeightsSnapshot
    validator_trust: tuple[int, ...]
    last_updates: tuple[int, ...]
    consensus: tuple[int, ...]
    last_epoch_block: int
    pending_epoch_at: int
    subnet_epoch_index: int
    pending: tuple[PendingWeightCommit, ...]
    tempo: int
    blocks_since_last_step: int
    # The stateful boundary that ends the snapshot's epoch, from the same
    # should_run_epoch simulation drand v2 and the Pylon image use.
    next_epoch_block: int

    def reveal_offset_blocks(self, commit: PendingWeightCommit) -> int | None:
        """Implied reveal block relative to the boundary ending the commit's epoch.

        A stateful (drand 2.0) commit lands at +3; a legacy tempo+1 commit
        made in the same epoch lands far negative. ``None`` when the commit
        is not from the current or previous epoch, or its timestamp is missing.
        """
        implied = commit.implied_reveal_block
        if implied is None:
            return None
        if commit.epoch == self.subnet_epoch_index:
            return implied - self.next_epoch_block
        if commit.epoch + 1 == self.subnet_epoch_index:
            return implied - self.last_epoch_block
        return None


def counter(value: object, maximum: int = 2**64 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid chain weight diagnostic counter")
    return value


def vector(value: object, maximum: int) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 65536:
        raise ValueError("invalid chain weight diagnostic vector")
    return tuple(counter(v, maximum) for v in value)


def predict_next_epoch_block(
    *,
    last_epoch_block: int,
    pending_epoch_at: int,
    tempo: int,
    blocks_since_last_step: int,
    current_block: int,
) -> int:
    """First block after ``current_block`` at which Subtensor steps the epoch.

    A port of ``should_run_epoch`` as simulated by bittensor-drand 2.0.0 and
    the Pylon image's ``ditto_pylon_epoch`` module: a pending owner epoch fires
    once its block is reached, ``BlocksSinceLastStep`` beyond ``MAX_TEMPO``
    fires immediately, otherwise ``tempo`` blocks after the last step. It
    deliberately does not model ``MaxEpochsPerBlock`` deferral, like drand.
    """
    if not 1 <= tempo <= MAX_TEMPO or last_epoch_block > current_block:
        raise ValueError("invalid epoch schedule for next-epoch prediction")
    since = blocks_since_last_step
    for block in range(current_block + 1, current_block + MAX_TEMPO + 3):
        since += 1
        if (
            (pending_epoch_at > 0 and block >= pending_epoch_at)
            or since > MAX_TEMPO
            or block - last_epoch_block >= tempo
        ):
            return block
    raise ValueError("epoch simulation exceeded budget")


async def read_weight_diagnostics(
    client: ChainClient, netuid: int
) -> WeightDiagnostics:
    from async_substrate_interface import AsyncSubstrateInterface

    try:
        async with asyncio.timeout(50):
            snapshot = await client.get_weights(netuid)
            # A separate read connection still uses the matrix's exact hash.
            # Never combine latest vTrust with an older cached weight matrix.
            async with AsyncSubstrateInterface(
                url=client._substrate_url()
            ) as substrate:

                async def read(name: str):
                    value = await substrate.query(
                        module="SubtensorModule",
                        storage_function=name,
                        params=[netuid],
                        block_hash=snapshot.block_hash,
                    )
                    return getattr(value, "value", value)

                (
                    trust,
                    updates,
                    consensus,
                    last,
                    pending_at,
                    index,
                    tempo,
                    blocks_since,
                ) = await asyncio.gather(
                    *(
                        read(name)
                        for name in (
                            "ValidatorTrust",
                            "LastUpdate",
                            "Consensus",
                            "LastEpochBlock",
                            "PendingEpochAt",
                            "SubnetEpochIndex",
                            "Tempo",
                            "BlocksSinceLastStep",
                        )
                    )
                )
                commits = await substrate.query_map(
                    module="SubtensorModule",
                    storage_function="TimelockedWeightCommits",
                    params=[netuid],
                    block_hash=snapshot.block_hash,
                    fully_exhaust=True,
                )
                pending: list[PendingWeightCommit] = []
                async for epoch, rows in commits:
                    for row in rows:
                        if len(row) != 4 or len(pending) >= 2560:
                            raise ValueError("pending weight evidence exceeds bound")
                        # Ciphertext is deliberately neither returned nor logged.
                        pending.append(
                            PendingWeightCommit(
                                hotkey=str(row[0]),
                                epoch=counter(epoch),
                                commit_block=counter(row[1]),
                                reveal_round=counter(row[3]),
                            )
                        )
                pending = await _attach_commit_timestamps(substrate, pending)
            return WeightDiagnostics(
                snapshot,
                vector(trust, 65535),
                vector(updates, 2**64 - 1),
                vector(consensus, 65535),
                counter(last),
                counter(pending_at),
                counter(index),
                tuple(pending),
                tempo=counter(tempo, MAX_TEMPO),
                blocks_since_last_step=counter(blocks_since),
                next_epoch_block=predict_next_epoch_block(
                    last_epoch_block=counter(last),
                    pending_epoch_at=counter(pending_at),
                    tempo=counter(tempo, MAX_TEMPO),
                    blocks_since_last_step=counter(blocks_since),
                    current_block=snapshot.block,
                ),
            )
    except Exception as error:
        raise ChainConnectionError(
            "validator weight diagnostics unavailable"
        ) from error


async def _attach_commit_timestamps(
    substrate, pending: list[PendingWeightCommit]
) -> list[PendingWeightCommit]:
    """Read ``Timestamp.Now`` at each commit block; a failed read leaves ``None``.

    One block-hash lookup and one storage read per pending commit, bounded by
    the 2560-row cap and a small concurrency limit. A pruned node that no
    longer holds an old commit block's state degrades that one row to
    ``implied_reveal_block = null`` instead of failing the whole diagnostic.
    """
    gate = asyncio.Semaphore(_TIMESTAMP_READ_CONCURRENCY)
    by_block: dict[int, int | None] = {}

    async def timestamp(block: int) -> int | None:
        async with gate:
            try:
                block_hash = await substrate.get_block_hash(block)
                value = await substrate.query(
                    module="Timestamp",
                    storage_function="Now",
                    params=[],
                    block_hash=block_hash,
                )
                # pallet_timestamp stores milliseconds.
                return counter(getattr(value, "value", value)) // 1000
            except Exception as error:  # noqa: BLE001 - one row degrades, not all
                logger.warning(
                    "commit block %d timestamp unavailable: %s", block, error
                )
                return None

    blocks = sorted({commit.commit_block for commit in pending})
    for block, value in zip(
        blocks, await asyncio.gather(*(timestamp(b) for b in blocks)), strict=True
    ):
        by_block[block] = value
    return [
        PendingWeightCommit(
            hotkey=commit.hotkey,
            epoch=commit.epoch,
            commit_block=commit.commit_block,
            reveal_round=commit.reveal_round,
            commit_block_timestamp=by_block[commit.commit_block],
        )
        for commit in pending
    ]
