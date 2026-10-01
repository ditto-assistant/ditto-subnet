"""Offline-pinned public policy and finalized guard for queued treasury weights.

No approval file is installed by this source change. Missing configuration
refuses every enforcing request. This module never loads keys or submits an
extrinsic; it runs immediately before the existing dispatch path.
"""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
from typing import Any

from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    verify_policy_approval,
    verify_public_signature,
)
from ditto_screening_protocol.treasury_enforcement import (
    EnforcingTreasuryPin,
    TreasuryFleetMember,
    require_treasury_weight_authority,
)
from ditto_screening_protocol.treasury_identity import (
    read_treasury_dispatch_observation,
)


def approved_policy() -> tuple[TreasuryPolicyApproval, str, str]:
    if os.environ.get("DITTO_TREASURY_WEIGHT_ENFORCEMENT", "false") != "true":
        raise ValueError("treasury transport guard is not armed")
    path = os.environ.get("DITTO_TREASURY_SHADOW_APPROVAL_FILE", "").strip()
    digest = os.environ.get("DITTO_TREASURY_APPROVED_POLICY_DIGEST", "").strip()
    collector_digest = os.environ.get(
        "DITTO_TREASURY_COLLECTOR_POLICY_DIGEST", ""
    ).strip()
    if not path or not digest or not collector_digest:
        raise ValueError("treasury transport is not configured")
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError("oversized public approval")
        approval = TreasuryPolicyApproval.model_validate_json(raw)
        verify_policy_approval(
            approval,
            expected_policy_digest=digest,
            expected_collector_policy_digest=collector_digest,
            verify_signature=verify_public_signature,
        )
    except (OSError, ValueError):
        raise ValueError("invalid offline treasury transport approval") from None
    return approval, digest, collector_digest


def require_legacy_dispatch_allowed() -> None:
    """Armed transports reject old cached-ledger and already queued legacy jobs."""
    from pylon_service.api._unstable.tasks import StopRetrying

    if os.environ.get("DITTO_TREASURY_WEIGHT_ENFORCEMENT", "false") != "false":
        raise StopRetrying("legacy weights disabled by treasury transport fence")


def capability() -> dict[str, Any] | None:
    try:
        _, digest, collector_digest = approved_policy()
    except ValueError:
        return None
    return {
        "treasury_pin_version": 2,
        "treasury_dispatch_version": 2,
        "approved_policy_digest": digest,
        "collector_policy_digest": collector_digest,
    }


def configured_request(
    pin: EnforcingTreasuryPin,
    *,
    netuid: int,
    validator_hotkey: str,
) -> tuple[TreasuryFleetMember, str, str]:
    pin = EnforcingTreasuryPin.model_validate(pin)
    approval, digest, collector_digest = approved_policy()
    if (
        approval.policy != pin.policy
        or type(netuid) is not int
        or netuid != pin.policy.netuid
    ):
        raise ValueError("queued treasury policy differs from deployment")
    member = next(
        (m for m in pin.fleet if m.validator_hotkey == validator_hotkey), None
    )
    if member is None:
        raise ValueError("queued treasury validator is absent from pinned fleet")
    return member, digest, collector_digest


def validate_service_vector(
    pin: EnforcingTreasuryPin, weights: dict[str, float]
) -> None:
    if not math.isclose(math.fsum(weights.values()), 1, rel_tol=0, abs_tol=1e-12):
        raise ValueError("treasury vector must conserve the complete emission")
    if not math.isclose(
        weights.get(pin.policy.collector_hotkey, 0),
        pin.policy.service_bps / 10_000,
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise ValueError("treasury vector differs from fixed service allocation")


def validate_normalized_service_vector(
    pin: EnforcingTreasuryPin, *, collector_uid: int, weights: dict[int, int]
) -> None:
    if any(type(value) is not int or value < 0 for value in weights.values()):
        raise ValueError("invalid normalized treasury weights")
    collector_weight = weights.get(collector_uid, 0)
    total = sum(weights.values())
    # Integer normalization may round an active allocation slightly. A paused
    # pool has no rounding allowance: it must receive exactly zero weight.
    if (
        not total
        or (pin.policy.service_bps == 0 and collector_weight != 0)
        or (pin.policy.service_bps > 0 and collector_weight <= 0)
        or not math.isclose(
            collector_weight / total,
            pin.policy.service_bps / 10_000,
            rel_tol=0,
            abs_tol=1e-4,
        )
    ):
        raise ValueError("normalized collector weight differs from approved pool")


class FinalizedReads:
    """Adapt the already connected TurboBT client's public read-only methods."""

    def __init__(self, client: Any):
        self.client = client

    async def get_chain_finalised_head(self) -> str:
        return await self.client.subtensor.rpc(
            method="chain_getFinalizedHead", params={}
        )

    async def get_block_number(self, block_hash: str) -> int:
        header = await self.client.subtensor.chain.getHeader(block_hash)
        if not isinstance(header, dict) or type(header.get("number")) is not int:
            raise ValueError("unreadable finalized header")
        return header["number"]

    async def get_block_hash(self, block_number: int) -> str:
        return await self.client.subtensor.chain.getBlockHash(block_number)

    async def query(self, **kwargs: Any) -> Any:
        return await self.client.subtensor.state.getStorage(
            f"{kwargs['module']}.{kwargs['storage_function']}",
            *kwargs["params"],
            block_hash=kwargs["block_hash"],
        )


async def require_queued_binding(
    client: Any,
    body: dict[str, Any],
    *,
    netuid: int,
    validator_hotkey: str,
):
    """Reverify deployment, crypto and current chain inside actual queued dispatch."""
    pin = EnforcingTreasuryPin.model_validate(body["treasury_pin"])
    member, digest, collector_digest = configured_request(
        pin,
        netuid=netuid,
        validator_hotkey=validator_hotkey,
    )
    validate_service_vector(pin, body["weights"])
    async with asyncio.timeout(8):
        observed = await read_treasury_dispatch_observation(
            FinalizedReads(client), pin.policy
        )
    require_treasury_weight_authority(
        pin,
        expected_policy_digest=digest,
        expected_collector_policy_digest=collector_digest,
        local_capability=member,
        current_identity=observed.identity,
        netuid=netuid,
        current_epoch_index=observed.epoch_index,
        current_first_block=observed.first_block,
        finalized_block=observed.finalized_block,
        finalized_block_hash=observed.finalized_block_hash,
    )
    return observed


async def queued_block(
    client: Any, body: dict[str, Any], netuid: int, validator_hotkey: str
):
    from pylon_commons.types import BlockHash, BlockNumber
    from pylon_service.api._unstable.tasks import StopRetrying
    from pylon_service.bittensor.models import Block

    try:
        observed = await require_queued_binding(
            client, body, netuid=netuid, validator_hotkey=validator_hotkey
        )
    except Exception:
        raise StopRetrying("treasury queued dispatch proof unavailable") from None
    return Block(
        number=BlockNumber(observed.finalized_block),
        hash=BlockHash(observed.finalized_block_hash),
    )


async def guard_normalized_commit(
    client: Any, netuid: int, weights: dict[int, int]
) -> None:
    """Repeat finalized binding immediately before the existing commit call."""
    from ditto_pylon_receipts import treasury_task_body
    from pylon_service.api._unstable.tasks import StopRetrying

    body, validator_hotkey = treasury_task_body()
    if body is None:
        require_legacy_dispatch_allowed()
        return
    try:
        observed = await require_queued_binding(
            client, body, netuid=netuid, validator_hotkey=validator_hotkey
        )
        pin = EnforcingTreasuryPin.model_validate(body["treasury_pin"])
        validate_normalized_service_vector(
            pin, collector_uid=observed.identity.uid, weights=weights
        )
    except Exception:
        raise StopRetrying("treasury normalized dispatch proof unavailable") from None
