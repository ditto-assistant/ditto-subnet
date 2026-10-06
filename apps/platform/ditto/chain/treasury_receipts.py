"""Read canonical finalized effects using the existing archive connection.

No journal field, caller boolean, balance delta or weight receipt is authority.
These reads prove money movement, not custody authorization or provider credits.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ditto.api_models.treasury_ingress import TreasuryReceiptSelector
from ditto_screening_protocol.collector_receipts import (
    AUDITED_COLLECTOR_RECEIPT_HASHES,
    FINNEY_GENESIS,
    chain_uint,
    collector_gross_incentive,
    collector_receipt_runtime,
    collector_transfer_effect,
    liquid_collector_credit,
)
from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_identity import read_finalized_collector_pin


def value(raw: Any) -> Any:
    return getattr(raw, "value", raw)


class _FinalizedReceiptSnapshot:
    """Reuse exact finality/hash reads only inside one receipt proof.

    All validation still runs. Storage, identity, events and effects are read
    normally; no proof or authority is cached across calls/providers. A single
    finalized head anchors the historical canonical blocks in this invocation.
    """

    def __init__(self, substrate: Any):
        self.substrate = substrate
        self.cache: dict[tuple[str, str], Any] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.substrate, name)

    async def _read(self, name: str, args: tuple, kwargs: dict) -> Any:
        key = (name, json.dumps((args, kwargs), sort_keys=True))
        if key not in self.cache:
            self.cache[key] = deepcopy(
                await getattr(self.substrate, name)(*args, **kwargs)
            )
        return deepcopy(self.cache[key])

    async def get_chain_finalised_head(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_chain_finalised_head", args, kwargs)

    async def get_block_number(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_block_number", args, kwargs)

    async def get_block_hash(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_block_hash", args, kwargs)

    async def rpc_request(self, method: str, params: list[Any], **kwargs: Any) -> Any:
        if (
            method == "state_getStorageHash"
            and len(params) == 2
            and params[0] == "0x3a636f6465"
        ):
            return await self._read("rpc_request", (method, params), kwargs)
        return await self.substrate.rpc_request(method, params, **kwargs)


@dataclass(frozen=True)
class TreasuryChainProof:
    source_block_hash: str | None
    source_amount_rao: int
    source_event_digest: str | None
    block_hash: str
    extrinsic_hash: str
    event_index: int
    event_at: datetime
    sender: str
    recipient: str
    asset: str
    amount_atomic: int
    runtime_code_hash: str


async def finalized_block(
    substrate: Any, block: int, genesis: str
) -> tuple[str, str, str]:
    """Fixed canonical hashes and audited execution/receipt runtime."""
    head = await substrate.get_chain_finalised_head()
    height = chain_uint(await substrate.get_block_number(head))
    if await substrate.get_block_hash(height) != head or not 0 < block <= height:
        raise ValueError("receipt block is not canonical finalized state")
    if genesis != FINNEY_GENESIS or await substrate.get_block_hash(0) != genesis:
        raise ValueError("receipt is on a different chain")
    at = await substrate.get_block_hash(block)
    parent = await substrate.get_block_hash(block - 1)
    hashes = []
    for pinned in (parent, at):
        if not pinned:
            raise ValueError("historical receipt block missing")
        response = await substrate.rpc_request(
            "state_getStorageHash", ["0x3a636f6465", pinned]
        )
        code = response.get("result") if isinstance(response, dict) else None
        if code not in AUDITED_COLLECTOR_RECEIPT_HASHES:
            raise ValueError("receipt runtime is not audited")
        hashes.append(code)
    return at, parent, collector_receipt_runtime(*hashes)


async def read_treasury_chain_proof(
    substrate: Any,
    selector: TreasuryReceiptSelector,
    policy: TreasuryEmissionPolicy,
    *,
    sender: str,
    recipient: str,
    asset: str,
    recipient_hotkey: str | None,
    pinned_block: int,
    pinned_block_hash: str,
    pinned_uid: int,
) -> TreasuryChainProof:
    substrate = _FinalizedReceiptSnapshot(substrate)
    pinned_at, _, _ = await finalized_block(
        substrate, pinned_block, policy.genesis_hash
    )
    if pinned_at != pinned_block_hash:
        raise ValueError("historical approved epoch hash is noncanonical")
    at, _, code = await finalized_block(substrate, selector.block, policy.genesis_hash)
    if at != selector.block_hash:
        raise ValueError("receipt hash changed or is noncanonical")
    # The historical approval identity is reread at its exact canonical pin.
    anchor = await read_finalized_collector_pin(
        substrate, policy, first_block=pinned_block, pinned_block=pinned_block
    )
    if anchor.identity.uid != pinned_uid:
        raise ValueError("canonical collector differs from immutable epoch identity")

    async def read(module: str, name: str, params: list[Any], block_hash: str) -> Any:
        return value(
            await substrate.query(
                module=module,
                storage_function=name,
                params=params,
                block_hash=block_hash,
            )
        )

    source_hash = None
    credit = None
    if selector.source_block is not None:
        source_hash, source_parent, _ = await finalized_block(
            substrate, selector.source_block, policy.genesis_hash
        )
        identities = []
        for block in sorted(
            {
                selector.source_block - 1,
                selector.source_block,
                selector.block - 1,
                selector.block,
            }
        ):
            pin = await read_finalized_collector_pin(
                substrate, policy, first_block=block, pinned_block=block
            )
            identities.append(
                (pin.identity.uid, pin.identity.hotkey, pin.identity.owner_coldkey)
            )
        approved_identity = (
            anchor.identity.uid,
            anchor.identity.hotkey,
            anchor.identity.owner_coldkey,
        )
        if any(identity != approved_identity for identity in identities):
            raise ValueError("collector identity drift in receipt history")
        epoch = chain_uint(
            await read("SubtensorModule", "SubnetEpochIndex", [118], source_hash)
        )
        if epoch != selector.epoch_index:
            raise ValueError("earning differs from historical epoch")
        for pinned in (source_parent, source_hash):
            route = await read(
                "SubtensorModule",
                "AutoStakeDestination",
                [policy.collector_coldkey, 118],
                pinned,
            )
            if route != policy.collector_hotkey:
                raise ValueError("collector earning route is not liquid SN118")
        source_events = await read("System", "Events", [], source_hash)
        if not isinstance(source_events, list):
            raise ValueError("source credit events unavailable")
        gross = collector_gross_incentive(source_events, identities[0][0])
        if gross is None:
            raise ValueError("collector gross emission absent")
        credit = liquid_collector_credit(
            source_events,
            collector_hotkey=policy.collector_hotkey,
            collector_coldkey=policy.collector_coldkey,
            gross_incentive_rao=gross,
        )
        if credit is None:
            raise ValueError("liquid collector credit absent")

    elif (
        chain_uint(await read("SubtensorModule", "SubnetEpochIndex", [118], at))
        != selector.epoch_index
    ):
        raise ValueError("vendor payment differs from historical policy epoch")

    raw = await substrate.rpc_request("chain_getBlock", [at])
    raw_block = raw.get("result", {}).get("block", {}) if isinstance(raw, dict) else {}
    encoded = raw_block.get("extrinsics")
    decoded = await substrate.get_block(block_hash=at)
    extrinsics = decoded.get("extrinsics") if isinstance(decoded, dict) else None
    index = selector.extrinsic_index
    if (
        not isinstance(encoded, list)
        or not isinstance(extrinsics, list)
        or len(encoded) != len(extrinsics)
        or index >= len(encoded)
    ):
        raise ValueError("receipt extrinsic unavailable")
    raw_extrinsic = encoded[index]
    if not isinstance(raw_extrinsic, str) or not raw_extrinsic.startswith("0x"):
        raise ValueError("receipt extrinsic encoding invalid")
    digest = (
        "0x"
        + hashlib.blake2b(bytes.fromhex(raw_extrinsic[2:]), digest_size=32).hexdigest()
    )
    if digest != selector.extrinsic_hash:
        raise ValueError("receipt extrinsic hash mismatch")
    extrinsic = value(extrinsics[index])
    call = extrinsic.get("call") if isinstance(extrinsic, dict) else None
    if not isinstance(call, dict):
        raise ValueError("receipt call unavailable")
    proxy = call.get("call_module") == "Proxy" and call.get("call_function") == "proxy"
    origin = extrinsic.get("address")
    if proxy:
        args = call.get("call_args")
        if not isinstance(args, list) or any(not isinstance(a, dict) for a in args):
            raise ValueError("proxy arguments unavailable")
        real = [a.get("value") for a in args if a.get("name") == "real"]
        if real != [sender]:
            raise ValueError("receipt proxy real origin differs from sender")
    elif origin != sender:
        raise ValueError("receipt signed origin differs from sender")
    events = await read("System", "Events", [], at)
    if not isinstance(events, list):
        raise ValueError("receipt events unavailable")
    if asset == "SN118_ALPHA":
        # This supported lane is the reviewed same-hotkey proxy transfer.
        if not proxy or recipient_hotkey != policy.collector_hotkey:
            raise ValueError("unsupported alpha payment lane")
        effect = collector_transfer_effect(
            events,
            extrinsic_index=index,
            collector_coldkey=sender,
            collector_hotkey=policy.collector_hotkey,
            recipient_coldkey=recipient,
            amount_rao=selector.amount_atomic,
        )
        event_index = effect.added_event_index
    elif asset == "TAO":
        scoped = [
            (i, e)
            for i, e in enumerate(events)
            if isinstance(e, dict)
            and e.get("phase") == "ApplyExtrinsic"
            and type(e.get("extrinsic_idx")) is int
            and e.get("extrinsic_idx") == index
        ]
        successes = [
            e
            for _, e in scoped
            if (e.get("module_id"), e.get("event_id")) == ("System", "ExtrinsicSuccess")
        ]
        failures = [
            e
            for _, e in scoped
            if (e.get("module_id"), e.get("event_id")) == ("System", "ExtrinsicFailed")
        ]
        if len(successes) != 1 or failures:
            raise ValueError("successful TAO dispatch absent")
        inner = [
            e.get("event", {}).get("attributes")
            for _, e in scoped
            if (e.get("module_id"), e.get("event_id")) == ("Proxy", "ProxyExecuted")
        ]
        if (
            proxy
            and inner not in ([{"result": {"Ok": None}}], [{"result": {"Ok": []}}])
        ) or (not proxy and inner):
            raise ValueError("successful inner TAO dispatch absent")
        transfers = [
            (i, e.get("event", {}).get("attributes"))
            for i, e in scoped
            if (e.get("module_id"), e.get("event_id")) == ("Balances", "Transfer")
        ]
        if len(transfers) != 1:
            raise ValueError("unique TAO transfer absent")
        event_index, attrs = transfers[0]
        if (
            not isinstance(attrs, dict)
            or set(attrs) != {"from", "to", "amount"}
            or attrs["from"] != sender
            or attrs["to"] != recipient
            or chain_uint(attrs["amount"]) != selector.amount_atomic
        ):
            raise ValueError("TAO transfer differs from exact payee rule")
    else:
        raise ValueError("asset effect decoder is not implemented")
    timestamp = chain_uint(await read("Timestamp", "Now", [], at))
    return TreasuryChainProof(
        source_hash,
        credit.amount_rao if credit else 0,
        credit.event_digest if credit else None,
        at,
        digest,
        event_index,
        datetime.fromtimestamp(timestamp / 1000, tz=UTC),
        sender,
        recipient,
        asset,
        selector.amount_atomic,
        code,
    )
