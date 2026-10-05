"""Bounded proposed-policy preflight using the existing full-fleet contract."""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import TypeAdapter
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_activation import (
    PREFLIGHT_ROW_LIMIT,
    TreasuryActivationPreflight,
    TreasuryActivationPreflightRequest,
    TreasuryPreflightBlockReason,
    TreasurySetterPreflight,
    TreasurySetterStatus,
)
from ditto.api_models.validator_capabilities import ValidatorCapabilities
from ditto.api_server.treasury_weights import (
    TREASURY_FLEET_FRESHNESS,
    treasury_fleet_members,
)
from ditto.db.models import ValidatorHeartbeat
from ditto_screening_protocol.treasury import Address, TreasuryLedgerPin
from ditto_screening_protocol.treasury_approval import (
    verify_policy_approval,
    verify_public_signature,
)
from ditto_screening_protocol.treasury_identity import TreasuryDispatchObservation


def setter_preflight(
    row: Any,
    *,
    hotkey: str,
    required: bool,
    now: datetime,
    policy_digest: str,
    collector_digest: str,
    inventory_complete: bool = True,
) -> TreasurySetterPreflight:
    seen_at = None
    protocol = None
    capability = None
    status: TreasurySetterStatus = (
        "missing_heartbeat" if inventory_complete else "inventory_not_checked"
    )
    if row is not None:
        seen_at = row.seen_at
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        protocol = row.protocol_version
        status = "invalid_heartbeat"
        try:
            capability = ValidatorCapabilities.model_validate_json(
                json.dumps(row.capabilities)
            ).treasury_weights
            if not now - TREASURY_FLEET_FRESHNESS <= seen_at <= now:
                status = "heartbeat_outside_window"
            elif capability is None:
                status = "missing_guard"
            elif type(protocol) is not int or protocol < 30:
                status = "unsupported_protocol"
            elif (
                capability.approved_policy_digest,
                capability.collector_policy_digest,
            ) != (policy_digest, collector_digest):
                status = "policy_mismatch"
            else:
                # Reuse the authoritative projection; diagnostics must not
                # invent a weaker definition of ready.
                treasury_fleet_members(
                    [row],
                    now=now,
                    policy_digest=policy_digest,
                    collector_digest=collector_digest,
                )
                status = "ready"
        except (ValueError, TypeError):
            pass
    return TreasurySetterPreflight(
        validator_hotkey=hotkey,
        required_by_chain=required,
        seen_at=seen_at,
        protocol_version=protocol,
        capability=capability,
        status=status,
    )


async def activation_preflight(
    state: Any,
    session: AsyncSession,
    payload: TreasuryActivationPreflightRequest,
    *,
    now: datetime,
) -> TreasuryActivationPreflight:
    # Reject invalid signatures before any chain read. Public-key verification
    # does not load a wallet or access signing credentials.
    policy = verify_policy_approval(
        payload.approval,
        expected_policy_digest=payload.expected_policy_digest,
        expected_collector_policy_digest=payload.expected_collector_policy_digest,
        verify_signature=verify_public_signature,
    )
    observation = None
    required: tuple[str, ...] = ()
    chain_status: Literal["verified", "unavailable"] = "unavailable"
    reasons: list[TreasuryPreflightBlockReason] = []
    try:
        observed = TreasuryDispatchObservation.model_validate(
            await state.chain.get_treasury_dispatch_observation(policy)
        )
        TreasuryLedgerPin(
            policy=policy, policy_digest=policy.digest, identity=observed.identity
        )
        keys = await state.chain.get_treasury_weight_setters(
            policy, block_hash=observed.finalized_block_hash
        )
        required = TypeAdapter(tuple[Address, ...]).validate_python(keys)
        if not required or len(set(required)) != len(required) or len(required) > 4096:
            raise ValueError("invalid authorization roster")
        observation = observed
        chain_status = "verified"
    except Exception:
        # Keep fixed, source-free diagnostics, never raw provider error text.
        required = ()
        reasons.append("chain_unavailable")
    # Preserve stale required rows and every fresh extra, as the enforcing
    # gate uses all fresh authenticated reports without a scorer filter.
    rows = list(
        await session.scalars(
            select(ValidatorHeartbeat)
            .where(
                or_(
                    ValidatorHeartbeat.validator_hotkey.in_(required),
                    ValidatorHeartbeat.seen_at >= now - TREASURY_FLEET_FRESHNESS,
                )
            )
            .order_by(ValidatorHeartbeat.validator_hotkey)
            .limit(PREFLIGHT_ROW_LIMIT + 1)
        )
    )
    inventory = {r.validator_hotkey: r for r in rows[:PREFLIGHT_ROW_LIMIT]}
    hotkeys = sorted(set(required) | set(inventory))
    truncated = len(rows) > PREFLIGHT_ROW_LIMIT or len(hotkeys) > PREFLIGHT_ROW_LIMIT
    setters = [
        setter_preflight(
            inventory.get(h),
            hotkey=h,
            required=h in required,
            now=now,
            policy_digest=policy.digest,
            collector_digest=policy.collector_policy_digest,
            inventory_complete=len(rows) <= PREFLIGHT_ROW_LIMIT,
        )
        for h in hotkeys[:PREFLIGHT_ROW_LIMIT]
    ]
    if truncated:
        reasons.append("inventory_truncated")
    if any(s.status != "ready" for s in setters):
        reasons.append("setter_proof_missing")
    config = state.config
    return TreasuryActivationPreflight(
        checked_at=now,
        proposed_policy_digest=policy.digest,
        proposed_collector_policy_digest=policy.collector_policy_digest,
        configured_policy_matches=(
            config.treasury_shadow_approval is not None
            and config.treasury_shadow_approval.policy == policy
            and config.treasury_approved_policy_digest == policy.digest
        ),
        configured_collector_matches=config.treasury_approved_collector_policy_digest
        == policy.collector_policy_digest,
        chain_status=chain_status,
        observation=observation,
        required_setter_count=len(required) if observation else None,
        setters=setters,
        truncated=truncated,
        fleet_ready_for_proposed_policy=not reasons,
        blocking_reasons=reasons,
    )
