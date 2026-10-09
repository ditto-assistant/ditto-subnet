"""Backend contract gate over the audited managed-validator roster.

No score/scorer filter participates in this gate. The explicit producer switch
is absent by default; an unavailable or mixed fleet must never downgrade an
enforcing epoch to the legacy fold.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.validator_capabilities import ValidatorCapabilities
from ditto.api_server.treasury_read_diagnostics import (
    record_treasury_read,
    treasury_read_failure_kind,
)
from ditto.chain.errors import (
    ChainTreasuryActivationReadError,
    ChainTreasuryReadTimeoutError,
)
from ditto.db.models import ValidatorHeartbeat
from ditto_screening_protocol.treasury import TreasuryLedgerPin
from ditto_screening_protocol.treasury_approval import (
    verify_policy_approval,
    verify_public_signature,
)
from ditto_screening_protocol.treasury_enforcement import (
    TREASURY_WEIGHT_PROTOCOL,
    EnforcingTreasuryPin,
    TreasuryFleetMember,
    require_treasury_weight_authority,
    treasury_follower_capability,
)
from ditto_screening_protocol.treasury_identity import (
    TreasuryDispatchObservation,
    TreasuryManagedSetterObservation,
)

TREASURY_FLEET_FRESHNESS = timedelta(minutes=15)


async def current_managed_weight_setters(
    chain: Any, policy: Any, *, block_hash: str, managed_hotkeys: tuple[str, ...]
) -> tuple[str, ...]:
    """Use fresh managed chain bindings; older readers retain the full gate."""
    if not managed_hotkeys or len(set(managed_hotkeys)) != len(managed_hotkeys):
        raise ValueError("managed roster is empty or ambiguous")
    scoped = getattr(chain, "get_treasury_managed_weight_setters", None)
    if callable(scoped):
        proof = TreasuryManagedSetterObservation.model_validate(
            await scoped(policy, block_hash=block_hash, managed_hotkeys=managed_hotkeys)
        )
        if proof.block_hash != block_hash or set(proof.hotkeys) != set(managed_hotkeys):
            raise ValueError("managed chain permission proof differs from pin scope")
        return proof.hotkeys
    return await chain.get_treasury_weight_setters(policy, block_hash=block_hash)


async def current_managed_dispatch_observation(
    chain: Any, policy: Any, *, managed_hotkeys: tuple[str, ...]
) -> TreasuryDispatchObservation:
    """Use one request-local transport; never reuse permission observations."""
    if not managed_hotkeys or len(set(managed_hotkeys)) != len(managed_hotkeys):
        raise ValueError("managed roster is empty or ambiguous")
    combined = getattr(chain, "get_treasury_managed_activation_observation", None)
    if callable(combined):
        observed, raw_proof = await combined(policy, managed_hotkeys=managed_hotkeys)
        try:
            observed = TreasuryDispatchObservation.model_validate(observed)
        except ValidationError as error:
            raise ChainTreasuryActivationReadError("identity", error) from error
        try:
            proof = TreasuryManagedSetterObservation.model_validate(raw_proof)
        except ValidationError as error:
            raise ChainTreasuryActivationReadError("setter_roster", error) from error
        if proof.block_hash != observed.finalized_block_hash or set(
            proof.hotkeys
        ) != set(managed_hotkeys):
            raise ValueError("managed permission proof differs from dispatch scope")
    else:
        try:
            observed = TreasuryDispatchObservation.model_validate(
                await chain.get_treasury_dispatch_observation(policy)
            )
        except ValidationError as error:
            raise ChainTreasuryActivationReadError("identity", error) from error
        except (ValueError, ChainTreasuryActivationReadError):
            # Preserve the legacy reader's explicit authority/evidence rejection.
            raise
        except Exception as error:
            raise ChainTreasuryActivationReadError("identity", error) from error
        try:
            required = await current_managed_weight_setters(
                chain,
                policy,
                block_hash=observed.finalized_block_hash,
                managed_hotkeys=managed_hotkeys,
            )
        except ValidationError as error:
            raise ChainTreasuryActivationReadError("setter_roster", error) from error
        except (ValueError, ChainTreasuryActivationReadError):
            raise
        except Exception as error:
            raise ChainTreasuryActivationReadError("setter_roster", error) from error
        if not required or not set(managed_hotkeys).issubset(required):
            raise ValueError("managed weight setter lacks current chain permission")
    return observed


def treasury_fleet_members(
    heartbeats: list[Any], *, now: datetime, policy_digest: str, collector_digest: str
) -> tuple[TreasuryFleetMember, ...]:
    """Refuse empty, stale, unknown or mismatched signed runtime evidence."""
    members = []
    for row in heartbeats:
        seen_at = row.seen_at
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        if not now - TREASURY_FLEET_FRESHNESS <= seen_at <= now:
            raise ValueError("treasury heartbeat is outside the freshness window")
        capabilities = ValidatorCapabilities.model_validate_json(
            json.dumps(row.capabilities)
        )
        capability = capabilities.treasury_weights
        if capability is None:
            raise ValueError("weight setter has no queued treasury guard")
        member = TreasuryFleetMember(
            **capability.model_dump(),
            validator_hotkey=row.validator_hotkey,
            protocol_version=row.protocol_version,
        )
        if (
            member.approved_policy_digest != policy_digest
            or member.collector_policy_digest != collector_digest
        ):
            raise ValueError("weight setter has a different approved policy")
        members.append(member)
    if not members or len({m.validator_hotkey for m in members}) != len(members):
        raise ValueError("treasury weight-setting fleet is empty or ambiguous")
    return tuple(sorted(members, key=lambda member: member.validator_hotkey))


async def read_treasury_fleet(
    session: AsyncSession,
    *,
    policy_digest: str,
    collector_digest: str,
    required_hotkeys: tuple[str, ...] = (),
) -> tuple[TreasuryFleetMember, ...]:
    """Validate the completed heartbeat inventory against its read-time clock."""
    if not required_hotkeys or len(set(required_hotkeys)) != len(required_hotkeys):
        raise ValueError("managed weight-setting roster is empty or ambiguous")
    rows = list(
        await session.scalars(
            select(ValidatorHeartbeat).where(
                ValidatorHeartbeat.validator_hotkey.in_(required_hotkeys)
            )
        )
    )
    # A server-stamped heartbeat may arrive while earlier chain/DB work is
    # awaited. Request or pin start time would misclassify it as future; it
    # could also accept evidence that expired while the read was in progress.
    checked_at = datetime.now(UTC)
    fleet = treasury_fleet_members(
        rows,
        now=checked_at,
        policy_digest=policy_digest,
        collector_digest=collector_digest,
    )
    if not set(required_hotkeys).issubset(
        {member.validator_hotkey for member in fleet}
    ):
        raise ValueError("managed or pinned weight setter has no fresh proof")
    return fleet


async def enforcing_pin_from_observation(
    app_state: Any,
    session: AsyncSession,
    shadow: TreasuryLedgerPin,
    schedule: Any,
    *,
    runtime: Any = None,
) -> EnforcingTreasuryPin:
    config = runtime if runtime is not None else app_state.config
    if (
        runtime is not None
        and runtime.activation_epoch is not None
        and schedule.subnet_epoch_index < runtime.activation_epoch
    ):
        raise ValueError("Gamma activation epoch has not begun")
    approval = config.treasury_shadow_approval
    policy = verify_policy_approval(
        approval,
        expected_policy_digest=config.treasury_approved_policy_digest,
        expected_collector_policy_digest=config.treasury_approved_collector_policy_digest,
        verify_signature=verify_public_signature,
    )
    if policy != shadow.policy:
        raise ValueError("finalized observation differs from approved policy")
    required_hotkeys = tuple(getattr(config, "treasury_managed_validator_hotkeys", ()))
    authorized = await current_managed_weight_setters(
        app_state.chain,
        policy,
        block_hash=shadow.identity.finalized_block_hash,
        managed_hotkeys=required_hotkeys,
    )
    if not required_hotkeys or not set(required_hotkeys).issubset(authorized):
        raise ValueError("managed weight setter lacks current chain permission")
    fleet = await read_treasury_fleet(
        session,
        policy_digest=policy.digest,
        collector_digest=policy.collector_policy_digest,
        required_hotkeys=required_hotkeys,
    )
    return EnforcingTreasuryPin(
        epoch_index=schedule.subnet_epoch_index,
        first_block=schedule.last_epoch_block,
        pinned_block=schedule.block,
        pinned_block_hash=schedule.block_hash,
        policy=policy,
        policy_digest=policy.digest,
        identity=shadow.identity,
        approval=approval,
        fleet=fleet,
    )


async def require_enforcing_requester(
    session: AsyncSession,
    pin: EnforcingTreasuryPin,
    hotkey: str,
    *,
    app_state: Any,
) -> None:
    from ditto.api_server.treasury_runtime import treasury_runtime

    config = await treasury_runtime(session, app_state.config)
    if config.revision and not config.treasury_weight_enforcement:
        raise ValueError("Gamma dispatch is paused or observation-only")
    if (
        config.activation_epoch is not None
        and pin.epoch_index < config.activation_epoch
    ):
        raise ValueError("enforcing pin predates activated runtime revision")
    managed = tuple(config.treasury_managed_validator_hotkeys)
    if not managed or set(managed) != {m.validator_hotkey for m in pin.fleet}:
        raise ValueError("managed roster differs from immutable pin")
    fleet = await read_treasury_fleet(
        session,
        policy_digest=pin.policy_digest,
        collector_digest=pin.policy.collector_policy_digest,
        required_hotkeys=tuple(member.validator_hotkey for member in pin.fleet),
    )
    if fleet != pin.fleet:
        raise ValueError("live managed fleet differs from immutable pin")
    member = next((m for m in fleet if m.validator_hotkey == hotkey), None)
    if member is None:
        # Managed activation is a producer gate, not a ledger-read allowlist.
        # Independent validators consume this same signed pin without needing
        # our deployment's approval file or joining our managed roster.
        row = await session.get(ValidatorHeartbeat, hotkey)
        checked_at = datetime.now(UTC)
        seen_at = row.seen_at if row is not None else None
        if seen_at is not None and seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        if (
            row is None
            or seen_at is None
            or not checked_at - TREASURY_FLEET_FRESHNESS <= seen_at <= checked_at
            or row.protocol_version < TREASURY_WEIGHT_PROTOCOL
        ):
            raise ValueError("treasury follower requires a fresh protocol 30 heartbeat")
        member = treasury_follower_capability(
            pin, validator_hotkey=hotkey, protocol_version=row.protocol_version
        )
    started = monotonic()
    try:
        observed = await current_managed_dispatch_observation(
            app_state.chain,
            pin.policy,
            managed_hotkeys=managed,
        )
    except ChainTreasuryActivationReadError as error:
        record_treasury_read(
            app_state,
            "requester_activation",
            elapsed=monotonic() - started,
            failure_kind=treasury_read_failure_kind(error.read_error),
            failure_stage=error.read_stage,
            failure_step=error.read_error.read_step
            if isinstance(error.read_error, ChainTreasuryReadTimeoutError)
            else None,
        )
        raise
    else:
        record_treasury_read(
            app_state, "requester_activation", elapsed=monotonic() - started
        )
    require_treasury_weight_authority(
        pin,
        expected_policy_digest=config.treasury_approved_policy_digest,
        expected_collector_policy_digest=config.treasury_approved_collector_policy_digest,
        local_capability=member,
        current_identity=observed.identity,
        netuid=app_state.config.chain.netuid,
        current_epoch_index=observed.epoch_index,
        current_first_block=observed.first_block,
        finalized_block=observed.finalized_block,
        finalized_block_hash=observed.finalized_block_hash,
    )
