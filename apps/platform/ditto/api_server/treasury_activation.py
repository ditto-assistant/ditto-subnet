"""Bounded proposed-policy preflight over the explicit managed-validator roster."""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import select
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
from ditto.chain.errors import ChainConnectionError, ChainTimeoutError
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
    from ditto.api_server.treasury_runtime import treasury_runtime

    try:
        config = await treasury_runtime(session, state.config)
    except ValueError:
        raise HTTPException(409, "Gamma runtime control is invalid") from None
    # The operator-controlled roster is durable; never derive membership from
    # whichever heartbeats happen to be fresh or from a self-reported stack label.
    required = tuple(
        sorted(
            payload.managed_validator_hotkeys
            or config.treasury_managed_validator_hotkeys
        )
    )
    authorized: tuple[str, ...] = ()
    observation = None
    chain_status: Literal["verified", "unavailable"] = "unavailable"
    reasons: list[TreasuryPreflightBlockReason] = []
    failure_stage: Literal["identity", "setter_roster"] | None = None
    failure_kind: (
        Literal[
            "timeout",
            "connection",
            "invalid_evidence",
            "reader_unavailable",
            "unavailable",
        ]
        | None
    ) = None
    if not required:
        reasons.append("managed_roster_missing")
    stage: Literal["identity", "setter_roster"] = "identity"
    try:
        observed = TreasuryDispatchObservation.model_validate(
            await state.chain.get_treasury_dispatch_observation(policy)
        )
        TreasuryLedgerPin(
            policy=policy, policy_digest=policy.digest, identity=observed.identity
        )
        stage = "setter_roster"
        keys = await state.chain.get_treasury_weight_setters(
            policy, block_hash=observed.finalized_block_hash
        )
        authorized = TypeAdapter(tuple[Address, ...]).validate_python(keys)
        if (
            not authorized
            or len(set(authorized)) != len(authorized)
            or len(authorized) > 4096
        ):
            raise ValueError("invalid authorization roster")
        observation = observed
        chain_status = "verified"
        if not set(required).issubset(authorized):
            reasons.append("managed_setter_not_permitted")
    except Exception as error:
        # Fixed labels identify the failed read without exposing provider URLs,
        # credentials or raw exception text. All failures remain non-authoritative.
        failure_stage = stage
        if isinstance(error, (TimeoutError, ChainTimeoutError)):
            failure_kind = "timeout"
        elif isinstance(error, (ConnectionError, ChainConnectionError)):
            failure_kind = "connection"
        elif isinstance(error, ValueError):
            failure_kind = "invalid_evidence"
        elif isinstance(error, AttributeError):
            failure_kind = "reader_unavailable"
        else:
            failure_kind = "unavailable"
        # Keep fixed, source-free diagnostics, never raw provider error text.
        reasons.append("chain_unavailable")
    # Include stale managed rows so a disappeared managed setter still blocks.
    # Independent validators are outside this operator's activation authority.
    rows = list(
        await session.scalars(
            select(ValidatorHeartbeat)
            .where(ValidatorHeartbeat.validator_hotkey.in_(required))
            .order_by(ValidatorHeartbeat.validator_hotkey)
            .limit(PREFLIGHT_ROW_LIMIT + 1)
        )
    )
    inventory = {r.validator_hotkey: r for r in rows[:PREFLIGHT_ROW_LIMIT]}
    truncated = len(rows) > PREFLIGHT_ROW_LIMIT or len(required) > PREFLIGHT_ROW_LIMIT
    setters = [
        setter_preflight(
            inventory.get(h),
            hotkey=h,
            required=h in authorized,
            now=now,
            policy_digest=policy.digest,
            collector_digest=policy.collector_policy_digest,
            inventory_complete=len(rows) <= PREFLIGHT_ROW_LIMIT,
        )
        for h in required[:PREFLIGHT_ROW_LIMIT]
    ]
    if truncated:
        reasons.append("inventory_truncated")
    if any(s.status != "ready" for s in setters):
        reasons.append("setter_proof_missing")
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
        chain_failure_stage=failure_stage,
        chain_failure_kind=failure_kind,
        observation=observation,
        required_setter_count=len(required) if required else None,
        managed_validator_hotkeys=required,
        chain_permitted_setter_count=len(authorized) if observation else None,
        setters=setters,
        truncated=truncated,
        fleet_ready_for_proposed_policy=not reasons,
        blocking_reasons=reasons,
    )
