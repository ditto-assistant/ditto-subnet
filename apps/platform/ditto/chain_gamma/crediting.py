"""Credit spends from finalized chain state (requirement 2).

At finalized block ``B``, for each publisher netuid, read
``C = RegisteredSubnetCounter(netuid)``. Then, for each publisher hotkey ``s``,
read every ``GammaSpent(netuid, s, u)`` and hand each total to the ledger, which
credits only growth. The storage total is the source of truth; ``GammaSpent``
events may only narrow which hotkeys to read.

The reader is a protocol. Production will implement it on Platform's existing
finalized-head substrate client (``ditto/chain``) with runtime-fingerprint pins
for the new storage items. That is a later PR; nothing here reads a chain.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

from ditto.chain_gamma.ledger import ChainGammaLedger, TotalKey, TotalObservation


@dataclass(frozen=True)
class FinalizedBlock:
    number: int
    block_hash: str


class ChainGammaReader(Protocol):
    async def finalized_block(self) -> FinalizedBlock:
        """``chain_getFinalizedHead``: a block that never reverts."""
        ...

    async def registered_subnet_counter(self, netuid: int, block_hash: str) -> int:
        """``SubtensorModule.RegisteredSubnetCounter(netuid)``; unset reads 0."""
        ...

    async def gamma_spent_at_service(
        self, netuid: int, service: str, block_hash: str
    ) -> Mapping[str, int]:
        """Every ``GammaSpent(netuid, service, *)`` total, by user hotkey."""
        ...

    async def gamma_spent(
        self, netuid: int, service: str, hotkey: str, block_hash: str
    ) -> int:
        """One ``GammaSpent(netuid, service, hotkey)`` total; unset reads 0."""
        ...


@dataclass(frozen=True)
class CreditReport:
    block: FinalizedBlock
    totals_read: int
    units_credited: int


async def credit_all(
    reader: ChainGammaReader,
    ledger: ChainGammaLedger,
    *,
    netuid: int,
    publisher_hotkeys: Collection[str],
    block: FinalizedBlock | None = None,
) -> CreditReport:
    """The periodic scan (at most 10 finalized blocks apart)."""
    at = block or await reader.finalized_block()
    counter = await reader.registered_subnet_counter(netuid, at.block_hash)
    read = credited = 0
    for service in sorted(publisher_hotkeys):
        totals = await reader.gamma_spent_at_service(netuid, service, at.block_hash)
        for user, total in sorted(totals.items()):
            read += 1
            credited += await ledger.observe_total(
                TotalObservation(
                    key=TotalKey(netuid, counter, service, user),
                    total_units=total,
                    block_number=at.number,
                    block_hash=at.block_hash,
                )
            )
    return CreditReport(block=at, totals_read=read, units_credited=credited)


async def credit_hotkey(
    reader: ChainGammaReader,
    ledger: ChainGammaLedger,
    *,
    netuid: int,
    publisher_hotkeys: Collection[str],
    hotkey: str,
    block: FinalizedBlock | None = None,
) -> int:
    """The on-demand read for one hotkey: on a balance request and before a 402."""
    at = block or await reader.finalized_block()
    counter = await reader.registered_subnet_counter(netuid, at.block_hash)
    credited = 0
    for service in sorted(publisher_hotkeys):
        total = await reader.gamma_spent(netuid, service, hotkey, at.block_hash)
        credited += await ledger.observe_total(
            TotalObservation(
                key=TotalKey(netuid, counter, service, hotkey),
                total_units=total,
                block_number=at.number,
                block_hash=at.block_hash,
            )
        )
    return credited
