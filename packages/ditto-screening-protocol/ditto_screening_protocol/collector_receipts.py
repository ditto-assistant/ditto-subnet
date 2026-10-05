"""Canonical audited v472 effect decoders, with no finality or money authority.

Both the signer journal and Platform's receipt reader must independently bind
the block, runtime fingerprint and historical identities before using these
decoders. A caller-supplied event list is not a verified chain receipt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

FINNEY_GENESIS = "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
# Exact finalized v472 :code hash; see docs/audits/collector-finney-v472/README.md.
AUDITED_COLLECTOR_CODE_HASH = (
    "0x43bc67be9df30636d7e948e7bdb1ed065f2fb92029458cc939abf89d76d8ada3"
)


def chain_uint(value: Any) -> int:
    value = getattr(value, "value", value)
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("invalid chain integer")
    return value


def _attributes(event: Mapping[str, Any]) -> Any:
    body = event.get("event")
    if not isinstance(body, Mapping):
        raise ValueError("unsupported event envelope")
    return body.get("attributes")


@dataclass(frozen=True)
class CollectorLiquidCredit:
    amount_rao: int
    event_index: int
    event_digest: str


@dataclass(frozen=True)
class CollectorTransferEffect:
    amount_rao: int
    removed_event_index: int
    added_event_index: int


def collector_gross_incentive(
    events: Sequence[Mapping[str, Any]], uid: int
) -> int | None:
    """Reviewed initialization event; gross is not liquid spending proof."""
    uid = chain_uint(uid)
    amounts = []
    for event in events:
        if (event.get("module_id"), event.get("event_id")) != (
            "SubtensorModule",
            "IncentiveAlphaEmittedToMiners",
        ):
            continue
        attrs = _attributes(event)
        if not isinstance(attrs, dict) or set(attrs) != {"netuid", "emissions"}:
            raise ValueError("unsupported emission event schema")
        if chain_uint(attrs["netuid"]) != 118:
            continue
        if (
            event.get("phase") != "Initialization"
            or not isinstance(attrs["emissions"], list)
            or uid >= len(attrs["emissions"])
        ):
            raise ValueError("ambiguous emission phase or missing collector UID")
        amounts.append(chain_uint(attrs["emissions"][uid]))
    if len(amounts) > 1:
        raise ValueError("ambiguous emission attribution")
    return amounts[0] if amounts else None


def liquid_collector_credit(
    events: Sequence[Mapping[str, Any]],
    *,
    collector_hotkey: str,
    collector_coldkey: str,
    gross_incentive_rao: int,
) -> CollectorLiquidCredit | None:
    """Decode actual liquid credit; gross incentive and balance deltas do not pay."""
    gross = chain_uint(gross_incentive_rao)
    matches: list[tuple[int, dict[str, Any]]] = []
    for index, event in enumerate(events):
        if (event.get("module_id"), event.get("event_id")) != (
            "SubtensorModule",
            "AutoStakeAdded",
        ):
            continue
        attrs = _attributes(event)
        if not isinstance(attrs, dict) or set(attrs) != {
            "netuid",
            "destination",
            "hotkey",
            "owner",
            "incentive",
        }:
            raise ValueError("unsupported liquid credit schema")
        if (chain_uint(attrs["netuid"]), attrs["hotkey"], attrs["owner"]) != (
            118,
            collector_hotkey,
            collector_coldkey,
        ):
            continue
        if (
            attrs["destination"] != collector_hotkey
            or event.get("phase") != "Initialization"
        ):
            raise ValueError("liquid credit redirected or ambiguous")
        matches.append((index, attrs))
    if len(matches) > 1:
        raise ValueError("ambiguous liquid emission credits")
    if not matches:
        return None
    index, attrs = matches[0]
    amount = chain_uint(attrs["incentive"])
    if amount > gross:
        raise ValueError("liquid credit exceeds gross collector incentive")
    if not amount:
        return None
    payload = json.dumps(attrs, sort_keys=True, separators=(",", ":"))
    return CollectorLiquidCredit(
        amount, index, hashlib.sha256(payload.encode()).hexdigest()
    )


def collector_transfer_effect(
    events: Sequence[Mapping[str, Any]],
    *,
    extrinsic_index: int,
    collector_coldkey: str,
    collector_hotkey: str,
    recipient_coldkey: str,
    amount_rao: int,
) -> CollectorTransferEffect:
    """Decode one successful exact same-SN118 proxy stake transfer effect."""
    extrinsic_index = chain_uint(extrinsic_index)
    amount = chain_uint(amount_rao)
    if not amount or collector_coldkey == recipient_coldkey:
        raise ValueError("positive distinct-recipient transfer required")
    for event in events:
        if event.get("phase") == "ApplyExtrinsic":
            chain_uint(event.get("extrinsic_idx"))
    scoped = [
        (i, e)
        for i, e in enumerate(events)
        if e.get("extrinsic_idx") == extrinsic_index
    ]
    if not scoped or any(e.get("phase") != "ApplyExtrinsic" for _, e in scoped):
        raise ValueError("extrinsic event phase differs from dispatch")

    def matching(module: str, name: str) -> list[tuple[int, Mapping[str, Any]]]:
        return [
            (i, e)
            for i, e in scoped
            if (e.get("module_id"), e.get("event_id")) == (module, name)
        ]

    if (
        matching("System", "ExtrinsicFailed")
        or len(matching("System", "ExtrinsicSuccess")) != 1
    ):
        raise ValueError("successful outer dispatch unproved")
    inner = matching("Proxy", "ProxyExecuted")
    if len(inner) != 1 or _attributes(inner[0][1]) not in (
        {"result": {"Ok": None}},
        {"result": {"Ok": []}},
        {"result": {"Ok": ()}},  # Pinned Linux SDK's decoded SCALE unit.
    ):
        raise ValueError("successful inner proxy dispatch unproved")
    removed = matching("SubtensorModule", "StakeRemoved")
    added = matching("SubtensorModule", "StakeAdded")
    if len(removed) != 1 or len(added) != 1:
        raise ValueError("missing or ambiguous alpha transfer effect")
    for (_, event), coldkey in (
        (removed[0], collector_coldkey),
        (added[0], recipient_coldkey),
    ):
        attrs = _attributes(event)
        if (
            not isinstance(attrs, (list, tuple))
            or len(attrs) != 6
            or attrs[0] != coldkey
            or attrs[1] != collector_hotkey
            or chain_uint(attrs[2]) < 0
            or chain_uint(attrs[3]) != amount
            or type(attrs[4]) is not int
            or attrs[4] != 118
            or chain_uint(attrs[5]) != 0
        ):
            raise ValueError("alpha transfer event differs from reserved intent")
    return CollectorTransferEffect(amount, removed[0][0], added[0][0])
