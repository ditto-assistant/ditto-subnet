"""Read the Gamma collector's public balance, lock and earnings at one block.

Every value comes from finalized public chain state, read at one block hash,
so a sample can be re-derived by anyone with an archive node. Nothing here
signs, moves funds or needs a credential.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# u16-normalized vectors (Incentive) and U64F64 conviction bits.
_U64F64_ONE = 1 << 64


@dataclass(frozen=True)
class TreasuryLedgerHolding:
    """One service bucket's holding coldkey at the sampled block."""

    bucket_id: str
    coldkey: str
    alpha_total_rao: int
    tao_free_rao: int


@dataclass(frozen=True)
class TreasuryLedgerReading:
    netuid: int
    epoch_index: int
    block_number: int
    block_hash: str
    block_timestamp_ms: int
    collector_hotkey: str
    collector_coldkey: str
    collector_uid: int | None
    collector_incentive: float | None
    """The collector UID's share of this subnet's incentive vector."""
    collector_emission_rao: int | None
    """Alpha emitted to the collector UID in the last epoch."""
    collector_total_rao: int
    collector_locked_rao: int
    collector_available_rao: int
    collector_lock_perpetual: bool | None
    """True for a perpetual conviction lock, False for a decaying one, None
    when the collector coldkey holds no lock."""
    owner_hotkey: str | None
    owner_conviction_rao: int | None
    most_convicted_hotkey: str | None
    holdings: tuple[TreasuryLedgerHolding, ...]


def _value(result: Any) -> Any:
    return None if result is None else getattr(result, "value", result)


def _as_map(value: Any) -> dict[Any, Any]:
    """SCALE BTreeMaps decode as a dict or, with several keys, as pairs."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        return dict(value)
    return {}


def _availability(raw: Any, coldkey: str, netuid: int) -> tuple[int, int, int]:
    """(total, locked, available) alpha rao from the availability runtime API."""
    by_coldkey = _as_map(_value(raw))
    by_netuid = _as_map(by_coldkey.get(coldkey))
    row = by_netuid.get(netuid) or by_netuid.get(str(netuid)) or {}
    return (
        int(row.get("total", 0)),
        int(row.get("locked", 0)),
        int(row.get("available", 0)),
    )


def _conviction_rao(raw: Any) -> int | None:
    value = _value(raw)
    if value is None:
        return None
    bits = value.get("bits") if isinstance(value, dict) else value
    return int(bits) // _U64F64_ONE if bits is not None else None


async def read_treasury_ledger(
    substrate: Any,
    *,
    netuid: int,
    collector_hotkey: str,
    collector_coldkey: str,
    holdings: Sequence[tuple[str, str]] = (),
) -> TreasuryLedgerReading:
    """Read one sample at the finalized head of ``substrate``."""
    block_hash = await substrate.get_chain_finalised_head()
    block_number = int(await substrate.get_block_number(block_hash))

    async def query(module: str, storage: str, params: list[Any]) -> Any:
        return _value(
            await substrate.query(module, storage, params, block_hash=block_hash)
        )

    async def runtime(method: str, params: list[Any]) -> Any:
        return await substrate.runtime_call(
            "StakeInfoRuntimeApi", method, params, block_hash=block_hash
        )

    epoch_index = int(await query("SubtensorModule", "SubnetEpochIndex", [netuid]))
    timestamp_ms = int(await query("Timestamp", "Now", []))
    uid_raw = await query("SubtensorModule", "Uids", [netuid, collector_hotkey])
    uid = int(uid_raw) if uid_raw is not None else None
    incentive: float | None = None
    emission: int | None = None
    if uid is not None:
        vector = await query("SubtensorModule", "Incentive", [netuid]) or []
        total = sum(int(item) for item in vector)
        if uid < len(vector) and total > 0:
            incentive = int(vector[uid]) / total
        emissions = await query("SubtensorModule", "Emission", [netuid]) or []
        if uid < len(emissions):
            emission = int(emissions[uid])

    coldkeys = [collector_coldkey, *(coldkey for _, coldkey in holdings)]
    availability = await runtime(
        "get_stake_availability_for_coldkeys",
        [list(dict.fromkeys(coldkeys)), [netuid]],
    )
    total_rao, locked_rao, available_rao = _availability(
        availability, collector_coldkey, netuid
    )
    perpetual: bool | None = None
    if locked_rao > 0:
        # DecayingLock stores False exactly when the lock is perpetual.
        perpetual = (
            await query("SubtensorModule", "DecayingLock", [collector_coldkey, netuid])
        ) is False

    owner_hotkey = await query("SubtensorModule", "SubnetOwnerHotkey", [netuid])
    owner_conviction = (
        _conviction_rao(await runtime("get_hotkey_conviction", [owner_hotkey, netuid]))
        if owner_hotkey
        else None
    )
    most_convicted = _value(
        await runtime("get_most_convicted_hotkey_on_subnet", [netuid])
    )

    held: list[TreasuryLedgerHolding] = []
    for bucket_id, coldkey in holdings:
        alpha_total, _, _ = _availability(availability, coldkey, netuid)
        account = await query("System", "Account", [coldkey]) or {}
        free = int((account.get("data") or {}).get("free", 0))
        held.append(
            TreasuryLedgerHolding(
                bucket_id=bucket_id,
                coldkey=coldkey,
                alpha_total_rao=alpha_total,
                tao_free_rao=free,
            )
        )

    return TreasuryLedgerReading(
        netuid=netuid,
        epoch_index=epoch_index,
        block_number=block_number,
        block_hash=str(block_hash).lower(),
        block_timestamp_ms=timestamp_ms,
        collector_hotkey=collector_hotkey,
        collector_coldkey=collector_coldkey,
        collector_uid=uid,
        collector_incentive=incentive,
        collector_emission_rao=emission,
        collector_total_rao=total_rao,
        collector_locked_rao=locked_rao,
        collector_available_rao=available_rao,
        collector_lock_perpetual=perpetual,
        owner_hotkey=str(owner_hotkey) if owner_hotkey else None,
        owner_conviction_rao=owner_conviction,
        most_convicted_hotkey=str(most_convicted) if most_convicted else None,
        holdings=tuple(held),
    )
