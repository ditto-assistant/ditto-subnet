"""Read-only finalized collector observations shared by chain consumers.

The caller supplies its existing read-only substrate connection. No wallet,
signer, configuration, environment or transaction method is accessed here.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator

from .treasury import (
    Address,
    Block,
    Hash,
    TreasuryCollectorIdentity,
    TreasuryEmissionPolicy,
    TreasuryLedgerPin,
)


async def read_finalized_weight_setters(
    substrate: CollectorReadClient,
    policy: TreasuryEmissionPolicy,
    *,
    block_hash: str,
) -> tuple[str, ...]:
    """All chain-permitted setters at the observation hash, even without scores.

    Missing keys or malformed permit vectors refuse activation; a heartbeat
    subset cannot stand in for the chain authorization roster.
    """
    permits = _value(
        await substrate.query(
            module="SubtensorModule",
            storage_function="ValidatorPermit",
            params=[policy.netuid],
            block_hash=block_hash,
        )
    )
    if (
        not isinstance(permits, (list, tuple))
        or not permits
        or len(permits) > 4096
        or any(type(value) is not bool for value in permits)
    ):
        raise ValueError("invalid chain weight-setter permit roster")
    keys = []
    for uid, permitted in enumerate(permits):
        if permitted:
            key = _value(
                await substrate.query(
                    module="SubtensorModule",
                    storage_function="Keys",
                    params=[policy.netuid, uid],
                    block_hash=block_hash,
                )
            )
            key = TypeAdapter(Address).validate_python(key)
            reciprocal_uid = _value(
                await substrate.query(
                    module="SubtensorModule",
                    storage_function="Uids",
                    params=[policy.netuid, key],
                    block_hash=block_hash,
                )
            )
            if type(reciprocal_uid) is not int or reciprocal_uid != uid:
                raise ValueError("weight-setter permit identity is not reciprocal")
            keys.append(key)
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("empty or ambiguous chain weight-setter roster")
    return tuple(sorted(keys))


class CollectorReadClient(Protocol):
    async def get_chain_finalised_head(self) -> str: ...
    async def get_block_number(self, block_hash: str) -> int: ...
    async def get_block_hash(self, block_number: int) -> str: ...
    async def query(self, **kwargs: Any) -> Any: ...


class TreasuryDispatchObservation(BaseModel):
    """Public storage observations at exactly one currently finalized hash."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, revalidate_instances="always"
    )

    identity: TreasuryCollectorIdentity
    epoch_index: Block
    first_block: Block
    finalized_block: Block
    finalized_block_hash: Hash

    @model_validator(mode="after")
    def one_finalized_hash(self) -> TreasuryDispatchObservation:
        if (self.identity.finalized_block, self.identity.finalized_block_hash) != (
            self.finalized_block,
            self.finalized_block_hash,
        ) or self.first_block > self.finalized_block:
            raise ValueError("treasury dispatch observation is not one finalized state")
        return self


async def read_treasury_dispatch_observation(
    substrate: CollectorReadClient,
    policy: TreasuryEmissionPolicy,
) -> TreasuryDispatchObservation:
    """Read identity and stateful epoch at the fresh finalized head, not a UID cache."""
    policy = TreasuryEmissionPolicy.model_validate(policy)
    block_hash = await substrate.get_chain_finalised_head()
    block = _uint(await substrate.get_block_number(block_hash))
    if await substrate.get_block_hash(block) != block_hash:
        raise ValueError("finalized treasury head hash is inconsistent")
    genesis = await substrate.get_block_hash(0)
    if genesis != policy.genesis_hash:
        raise ValueError("treasury dispatch chain differs from policy")

    async def read(storage: str, params: list[Any]) -> Any:
        return _value(
            await substrate.query(
                module="SubtensorModule",
                storage_function=storage,
                params=params,
                block_hash=block_hash,
            )
        )

    epoch = _uint(await read("SubnetEpochIndex", [policy.netuid]))
    first = _uint(await read("LastEpochBlock", [policy.netuid]))
    owner = await read("Owner", [policy.collector_hotkey])
    subnet_owner = await read("SubnetOwner", [policy.netuid])
    uid = _uint(await read("Uids", [policy.netuid, policy.collector_hotkey]))
    uid_hotkey = await read("Keys", [policy.netuid, uid])
    return TreasuryDispatchObservation(
        identity=TreasuryCollectorIdentity(
            genesis_hash=genesis,
            netuid=policy.netuid,
            finalized_block=block,
            finalized_block_hash=block_hash,
            uid=uid,
            hotkey=policy.collector_hotkey,
            uid_hotkey=uid_hotkey,
            owner_coldkey=owner,
            subnet_owner_coldkey=subnet_owner,
        ),
        epoch_index=epoch,
        first_block=first,
        finalized_block=block,
        finalized_block_hash=block_hash,
    )


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
