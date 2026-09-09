"""Stateful Subtensor epoch reads for the pinned Pylon/TurboBT image.

The chain's epoch counter and anchor are authoritative. Neither a validator's
wall clock nor the obsolete (block + netuid + 1) // (tempo + 1) formula may
choose a commitment's reveal epoch or a persisted weight task's lifetime.
Encryption and the three-block security offset remain owned by drand v2.

The epoch predicate below is a line-for-line port of bittensor-drand 2.0.0
``src/epoch_schedule.rs`` (itself a port of Subtensor ``run_coinbase.rs``
``should_run_epoch`` and ``weights.rs`` ``current_epoch_with_lookahead``).
Pylon's task-expiry window and drand's reveal target are therefore computed
from one model instead of a two-rule approximation that could disagree by a
block after a deferred, manually triggered, or safety-net epoch.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from typing import Any

SCHEDULE_STORAGE = {
    "last_epoch_block": "LastEpochBlock",
    "pending_epoch_at": "PendingEpochAt",
    "subnet_epoch_index": "SubnetEpochIndex",
    "tempo": "Tempo",
    "blocks_since_last_step": "BlocksSinceLastStep",
}
SCHEDULE_VERSION = "subtensor-stateful-epochs-v1"

# bittensor-drand 2.0.0 src/constants.rs. MAX_TEMPO is Subtensor's owner-set
# tempo ceiling and doubles as the BlocksSinceLastStep safety net.
MAX_TEMPO = 50_400
# The commit extrinsic lands one block after the head drand was given.
COMMIT_INCLUSION_BLOCK_OFFSET = 1
# drand targets a pulse three blocks after the first legal reveal block so the
# pulse is already ingested on-chain when the reveal runs.
SECURITY_BLOCK_OFFSET = 3


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
        if (
            not 1 <= self.tempo <= MAX_TEMPO
            or self.last_epoch_block > self.current_block
        ):
            raise ValueError("invalid stateful epoch anchor or tempo")

    # --- Verbatim drand 2.0.0 / Subtensor epoch predicate -------------------

    def should_run_epoch(self, block: int) -> bool:
        """Port of ``run_coinbase.rs`` ``should_run_epoch`` for ``block``.

        Evaluated against this state as it stands *before* ``run_coinbase`` at
        ``block``. Tempo zero cannot occur here; ``__post_init__`` rejects it.
        """
        if self.pending_epoch_at > 0 and block >= self.pending_epoch_at:
            return True
        if self.blocks_since_last_step > MAX_TEMPO:
            return True
        return max(block - self.last_epoch_block, 0) >= self.tempo

    def current_epoch_pre_run_coinbase(self, block: int) -> int:
        """Port of ``weights.rs`` ``current_epoch_with_lookahead``.

        ``reveal_crv3_commits`` runs before ``run_coinbase`` in ``block_step``
        and therefore sees the about-to-fire epoch, as does a commit extrinsic
        included on a fire block.
        """
        return self.subnet_epoch_index + (1 if self.should_run_epoch(block) else 0)

    def simulate_run_coinbase(self, block: int) -> EpochSchedule:
        """Port of drand's ``simulate_run_coinbase`` (``run_coinbase.rs`` subset).

        Does not model ``MaxEpochsPerBlock`` deferral, exactly like drand.
        """
        state = replace(
            self,
            blocks_since_last_step=self.blocks_since_last_step + 1,
            current_block=block,
        )
        if state.should_run_epoch(block):
            state = replace(
                state,
                last_epoch_block=block,
                pending_epoch_at=0,
                subnet_epoch_index=state.subnet_epoch_index + 1,
                blocks_since_last_step=0,
            )
        return state

    def advance_blocks(self, start: int, end: int) -> EpochSchedule:
        state = self
        for block in range(start, end + 1):
            state = state.simulate_run_coinbase(block)
        return state

    def predict_first_reveal_block(self, reveal_period_epochs: int) -> int:
        """Port of drand's ``predict_first_reveal_block``.

        The first block whose pre-``run_coinbase`` epoch equals the commit's
        epoch plus ``reveal_period_epochs``. drand adds ``SECURITY_BLOCK_OFFSET``
        to this and derives the drand round from wall-clock time.
        """
        if type(reveal_period_epochs) is not int or reveal_period_epochs < 0:
            raise ValueError("invalid reveal period")
        extrinsic_block = self.current_block + COMMIT_INCLUSION_BLOCK_OFFSET
        before_extrinsic = self.advance_blocks(
            self.current_block + 1, extrinsic_block - 1
        )
        commit_epoch = before_extrinsic.current_epoch_pre_run_coinbase(extrinsic_block)
        target_epoch = commit_epoch + reveal_period_epochs
        budget = reveal_period_epochs * MAX_TEMPO + MAX_TEMPO
        state = before_extrinsic
        for block in range(extrinsic_block, extrinsic_block + budget + 1):
            if state.current_epoch_pre_run_coinbase(block) == target_epoch:
                return block
            state = state.simulate_run_coinbase(block)
        raise ValueError("reveal block simulation exceeded budget")

    def target_ingest_block(self, reveal_period_epochs: int) -> int:
        """The block whose drand pulse a v2 commit from this state targets."""
        return (
            self.predict_first_reveal_block(reveal_period_epochs)
            + SECURITY_BLOCK_OFFSET
        )

    # --- Pylon task window -------------------------------------------------

    @property
    def next_epoch_block(self) -> int:
        """First block after ``current_block`` at which ``run_coinbase`` steps.

        Derived by the same simulation drand uses for the reveal target, so a
        persisted Pylon task's window cannot disagree with the encrypted
        commitment's epoch. This is the block that *fires*; the last block of
        the current epoch is one before it.
        """
        state = self
        for block in range(self.current_block + 1, self.current_block + MAX_TEMPO + 3):
            state = state.simulate_run_coinbase(block)
            if state.subnet_epoch_index != self.subnet_epoch_index:
                return block
        raise ValueError("epoch simulation exceeded budget")

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
