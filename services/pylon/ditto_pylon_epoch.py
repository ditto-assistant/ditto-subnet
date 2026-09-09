"""Stateful Subtensor epoch reads for the pinned Pylon/TurboBT image.

The chain's epoch counter and anchor are authoritative. Neither a validator's
wall clock nor the obsolete (block + netuid + 1) // (tempo + 1) formula may
choose a commitment's reveal epoch or a persisted weight task's lifetime.
Encryption and the three-block security offset remain owned by drand v2.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

SCHEDULE_STORAGE = {
    "last_epoch_block": "LastEpochBlock",
    "pending_epoch_at": "PendingEpochAt",
    "subnet_epoch_index": "SubnetEpochIndex",
    "tempo": "Tempo",
    "blocks_since_last_step": "BlocksSinceLastStep",
}
SCHEDULE_VERSION = "subtensor-stateful-epochs-v1"


@dataclass(frozen=True)
class EpochSchedule:
    last_epoch_block: int
    pending_epoch_at: int
    subnet_epoch_index: int
    tempo: int
    blocks_since_last_step: int
    current_block: int

    def __post_init__(self) -> None:
        if any(type(v) is not int or not 0 <= v < 2**64 for v in asdict(self).values()):
            raise ValueError("invalid stateful epoch counters")
        if not 1 <= self.tempo <= 50_400 or self.last_epoch_block > self.current_block:
            raise ValueError("invalid stateful epoch anchor or tempo")

    @property
    def next_epoch_block(self) -> int:
        automatic = self.last_epoch_block + self.tempo
        return (
            min(automatic, self.pending_epoch_at)
            if self.pending_epoch_at
            else automatic
        )

    def drand_arguments(self) -> dict[str, int]:
        return asdict(self)


async def read_epoch_schedule(
    client: Any, netuid: int, block_number: int, block_hash: str
) -> EpochSchedule:
    """Read every schedule field at one explicit block; missing data is an error.

    ``client`` is the existing wallet-bound TurboBT client. These are public
    storage reads through its transport; no new connection or credential exists.
    Schedule storage is subnet-scoped even for a nonzero weight mechanism.
    """
    if type(netuid) is not int or not 0 <= netuid <= 65535 or not block_hash:
        raise ValueError("invalid epoch read identity")
    async with asyncio.timeout(20):
        values = await asyncio.gather(
            *(
                client.subtensor.state.getStorage(
                    f"SubtensorModule.{storage}", netuid, block_hash=block_hash
                )
                for storage in SCHEDULE_STORAGE.values()
            )
        )
    return EpochSchedule(
        **dict(zip(SCHEDULE_STORAGE, values, strict=True)),
        current_block=block_number,
    )
