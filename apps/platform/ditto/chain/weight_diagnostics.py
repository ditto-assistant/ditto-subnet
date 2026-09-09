"""Bounded public chain evidence for vTrust; never a weight-setting path."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainWeightsSnapshot

if TYPE_CHECKING:
    from ditto.chain.client import ChainClient


@dataclass(frozen=True)
class PendingWeightCommit:
    hotkey: str
    epoch: int
    commit_block: int
    reveal_round: int


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


def counter(value: object, maximum: int = 2**64 - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid chain weight diagnostic counter")
    return value


def vector(value: object, maximum: int) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 65536:
        raise ValueError("invalid chain weight diagnostic vector")
    return tuple(counter(v, maximum) for v in value)


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
            return WeightDiagnostics(
                snapshot,
                vector(trust, 65535),
                vector(updates, 2**64 - 1),
                vector(consensus, 65535),
                counter(last),
                counter(pending_at),
                counter(index),
                tuple(pending),
            )
    except Exception as error:
        raise ChainConnectionError(
            "validator weight diagnostics unavailable"
        ) from error
