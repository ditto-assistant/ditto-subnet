"""Read-only finalized collector observations shared by chain consumers.

The caller supplies its existing read-only substrate connection. No wallet,
signer, configuration, environment or transaction method is accessed here.
"""

from __future__ import annotations

from typing import Any, Protocol

from .treasury import (
    TreasuryCollectorIdentity,
    TreasuryEmissionPolicy,
    TreasuryLedgerPin,
)


class CollectorReadClient(Protocol):
    async def get_chain_finalised_head(self) -> str: ...
    async def get_block_number(self, block_hash: str) -> int: ...
    async def get_block_hash(self, block_number: int) -> str: ...
    async def query(self, **kwargs: Any) -> Any: ...


def _value(raw: Any) -> Any:
    return getattr(raw, "value", raw)


def _uint(raw: Any) -> int:
    value = _value(raw)
    if type(value) is not int or value < 0:
        raise ValueError("collector observation is not an unsigned integer")
    return value


async def read_finalized_collector_pin(
    substrate: CollectorReadClient,
    policy: TreasuryEmissionPolicy,
    *,
    first_block: int,
    pinned_block: int,
) -> TreasuryLedgerPin:
    """Bind all storage reads to one finalized hash within this ledger epoch.

    A missing, stale, future or inconsistent read raises. This provides chain
    observations for a SHADOW pin; it does not verify offline policy approval.
    """
    policy = TreasuryEmissionPolicy.model_validate(policy)
    if any(type(b) is not int or b < 0 for b in (first_block, pinned_block)):
        raise ValueError("ledger epoch heights must be unsigned integers")
    if first_block > pinned_block:
        raise ValueError("invalid ledger epoch range")
    finalized_hash = await substrate.get_chain_finalised_head()
    finalized_block = _uint(await substrate.get_block_number(finalized_hash))
    block = min(finalized_block, pinned_block)
    if block < first_block:
        raise ValueError("collector finalized identity is stale for this epoch")
    block_hash = await substrate.get_block_hash(block)
    if block == finalized_block and block_hash != finalized_hash:
        raise ValueError("collector finalized hash is inconsistent")
    genesis_hash = await substrate.get_block_hash(0)
    if genesis_hash != policy.genesis_hash:
        raise ValueError("collector chain differs from policy")

    async def read(storage: str, params: list[Any]) -> Any:
        return _value(
            await substrate.query(
                module="SubtensorModule",
                storage_function=storage,
                params=params,
                block_hash=block_hash,
            )
        )

    owner = await read("Owner", [policy.collector_hotkey])
    subnet_owner = await read("SubnetOwner", [policy.netuid])
    uid = _uint(await read("Uids", [policy.netuid, policy.collector_hotkey]))
    uid_hotkey = await read("Keys", [policy.netuid, uid])
    identity = TreasuryCollectorIdentity(
        genesis_hash=genesis_hash,
        netuid=policy.netuid,
        finalized_block=block,
        finalized_block_hash=block_hash,
        uid=uid,
        hotkey=policy.collector_hotkey,
        owner_coldkey=owner,
        uid_hotkey=uid_hotkey,
        subnet_owner_coldkey=subnet_owner,
    )
    pin = TreasuryLedgerPin(
        policy=policy, policy_digest=policy.digest, identity=identity
    )
    pin.require_epoch(
        netuid=policy.netuid, first_block=first_block, pinned_block=pinned_block
    )
    return pin
