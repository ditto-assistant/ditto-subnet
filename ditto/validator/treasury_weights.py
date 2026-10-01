"""Service-first fold after crypto, epoch, fleet and finalized identity checks.

This module cannot submit weights. The worker and guarded Pylon transport must
repeat current identity checks before dispatch, and must never fall back to a
legacy transport after an enforcing pin is encountered.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypedDict

from ditto.treasury.service_allocation import service_first_weights
from ditto_screening_protocol.treasury import TreasuryCollectorIdentity
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


class TreasuryWeightAuthority(TypedDict):
    pin: EnforcingTreasuryPin
    expected_policy_digest: str
    expected_collector_policy_digest: str
    local_capability: TreasuryFleetMember
    current_identity: TreasuryCollectorIdentity
    netuid: int
    current_epoch_index: int
    current_first_block: int
    finalized_block: int
    finalized_block_hash: str


def configured_treasury_approval(config: Any) -> TreasuryPolicyApproval | None:
    """Public-only deploy proof; absence never advertises enforcing capability."""
    path = getattr(config, "treasury_approval_file", None)
    digest = getattr(config, "treasury_approved_policy_digest", None)
    collector_digest = getattr(config, "treasury_collector_policy_digest", None)
    if not any((path, digest, collector_digest)):
        return None
    if not all((path, digest, collector_digest)) or not isinstance(path, str):
        raise ValueError("treasury approval configuration is incomplete")
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError("oversized treasury approval")
        approval = TreasuryPolicyApproval.model_validate_json(raw)
        verify_policy_approval(
            approval,
            expected_policy_digest=digest,
            expected_collector_policy_digest=collector_digest,
            verify_signature=verify_public_signature,
        )
    except (OSError, ValueError):
        raise ValueError("invalid public treasury approval") from None
    return approval


def fold_treasury_weights(
    weights: Mapping[str, float],
    *,
    pin: EnforcingTreasuryPin,
    burn_share: float,
    paid_miner_fraction: float,
    burn_hotkey: str,
    expected_policy_digest: str,
    expected_collector_policy_digest: str,
    local_capability: TreasuryFleetMember,
    current_identity: TreasuryCollectorIdentity,
    netuid: int,
    current_epoch_index: int,
    current_first_block: int,
    finalized_block: int,
    finalized_block_hash: str,
) -> dict[str, float]:
    """Fold the raw blended miner vector exactly once, before any legacy cap."""
    validated = require_treasury_weight_authority(
        pin,
        expected_policy_digest=expected_policy_digest,
        expected_collector_policy_digest=expected_collector_policy_digest,
        local_capability=local_capability,
        current_identity=current_identity,
        netuid=netuid,
        current_epoch_index=current_epoch_index,
        current_first_block=current_first_block,
        finalized_block=finalized_block,
        finalized_block_hash=finalized_block_hash,
    )
    if burn_hotkey == validated.policy.collector_hotkey:
        raise ValueError("collector cannot be the burn destination")
    return service_first_weights(
        weights,
        service_bps=validated.policy.service_bps,
        burn_share=burn_share,
        paid_miner_fraction=paid_miner_fraction,
        collector_hotkey=validated.policy.collector_hotkey,
        collector_verified=True,  # Derived above; never supplied as authority.
        burn_hotkey=burn_hotkey,
    )
