"""Optional finalized shadow producer; no effective policy or weight authority."""

from __future__ import annotations

from typing import Any, Literal

from ditto.api_models.treasury_readiness import (
    ObserverStatus,
    TreasuryBlockReason,
    TreasuryLedgerReadiness,
)
from ditto.chain.models import EpochSchedule
from ditto_screening_protocol.treasury import TreasuryLedgerPin
from ditto_screening_protocol.treasury_approval import (
    verify_policy_approval,
    verify_public_signature,
)
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin


async def observe_shadow_treasury(
    app_state: Any, schedule: EpochSchedule, *, runtime: Any = None
) -> TreasuryLedgerPin | None:
    config = runtime if runtime is not None else app_state.config
    policy = getattr(config, "treasury_shadow_policy", None)
    if policy is None:
        app_state.treasury_shadow_observer_status = "disabled"
        return None
    app_state.treasury_shadow_observer_status = "observing"
    try:
        pin = await app_state.chain.get_treasury_collector_pin(
            policy, first_block=schedule.last_epoch_block, pinned_block=schedule.block
        )
        pin = TreasuryLedgerPin.model_validate(pin)
        if pin.policy != policy:
            raise ValueError(
                "observed treasury policy differs from configured proposal"
            )
        pin.require_epoch(
            netuid=schedule.netuid,
            first_block=schedule.last_epoch_block,
            pinned_block=schedule.block,
        )
        if (
            pin.identity.finalized_block == schedule.block
            and pin.identity.finalized_block_hash != schedule.block_hash
        ):
            raise ValueError("observed treasury hash differs from ledger block")
    except Exception:
        app_state.treasury_shadow_observer_status = "unavailable"
        raise
    app_state.treasury_shadow_observer_status = "observed"
    return pin


def shadow_readiness(
    app_state: Any, ledger_pin: Any | None, *, runtime: Any = None
) -> TreasuryLedgerReadiness:
    """Separate configured intent, stored evidence and process-local observation."""
    from ditto.api_server.ledger_pin import LedgerPin, treasury_pin_from_context

    config = runtime if runtime is not None else app_state.config
    policy = getattr(config, "treasury_shadow_policy", None)
    raw_status = getattr(app_state, "treasury_shadow_observer_status", "not_observed")
    approval = getattr(config, "treasury_shadow_approval", None)
    approval_status: Literal["not_configured", "verified", "invalid"] = "not_configured"
    approved_digest = None
    if approval is not None:
        try:
            approved_policy = verify_policy_approval(
                approval,
                expected_policy_digest=config.treasury_approved_policy_digest,
                expected_collector_policy_digest=(
                    config.treasury_approved_collector_policy_digest
                ),
                verify_signature=verify_public_signature,
            )
            if approved_policy != policy:
                raise ValueError("approval differs from configured proposal")
            approval_status = "verified"
            approved_digest = approved_policy.digest
        except (ValueError, TypeError, AttributeError):
            approval_status = "invalid"
    statuses: dict[str, ObserverStatus] = {
        "disabled": "disabled",
        "not_observed": "not_observed",
        "observing": "observing",
        "observed": "observed",
        "unavailable": "unavailable",
    }
    status: ObserverStatus = (
        statuses.get(raw_status, "unavailable")
        if isinstance(raw_status, str)
        else "unavailable"
    )
    enforcing = getattr(config, "treasury_weight_enforcement", False)
    reasons: list[TreasuryBlockReason] = ["current_epoch_not_checked"]
    if not enforcing:
        reasons.extend(
            ["shadow_only", "offline_policy_unverified", "weight_adapter_not_active"]
        )
    if policy is None:
        status = "disabled"
        reasons.append("producer_disabled")
    stored = None
    if ledger_pin is not None:
        try:
            # Validate before from_row can coerce a malformed JSON container.
            if not isinstance(ledger_pin.context, dict):
                raise ValueError("stored ledger context must be an object")
            projection = (
                ledger_pin
                if isinstance(ledger_pin, LedgerPin)
                else LedgerPin.from_row(ledger_pin)
            )
            stored = treasury_pin_from_context(projection)
        except (ValueError, TypeError, KeyError):
            reasons.append("stored_pin_invalid")
        if stored is not None and stored.policy != policy:
            reasons.append("proposal_pin_mismatch")
    if stored is None:
        reasons.append("no_epoch_pin")
    return TreasuryLedgerReadiness(
        configured_proposal=policy,
        proposal_approval_status=approval_status,
        proposal_approved_policy_digest=approved_digest,
        observer_status=status,
        latest_stored_epoch_index=ledger_pin.epoch_index if ledger_pin else None,
        latest_stored_ledger_digest=ledger_pin.ledger_digest if ledger_pin else None,
        stored_shadow_pin=stored if isinstance(stored, TreasuryLedgerPin) else None,
        stored_enforcing_pin=stored
        if isinstance(stored, EnforcingTreasuryPin)
        else None,
        enforcement_configured=enforcing,
        blocking_reasons=reasons,
    )
