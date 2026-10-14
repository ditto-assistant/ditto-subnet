"""Verified activity, separate from spending and provider credit authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_ingress import (
    TreasuryReceiptPreflight,
    TreasuryReceiptResult,
    TreasuryReceiptSelector,
)
from ditto.api_models.treasury_settings import TreasurySettings
from ditto.api_server.ledger_pin import LedgerPin, treasury_pin_from_context
from ditto.api_server.treasury_runtime import settings_match_policy
from ditto.db.models import (
    LedgerEpochSnapshot,
    TreasuryPublicEvent,
    TreasurySettingsRevision,
    TreasuryVerifiedReceipt,
)
from ditto_screening_protocol.treasury_approval import (
    verify_policy_approval,
    verify_public_signature,
)
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin
from ditto_screening_protocol.treasury_weight_math import (
    ServiceDestination,
    plan_service_distribution,
)


def digest(body: Any) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReceiptConflict(ValueError):
    """Replay or historical evidence does not match the immutable receipt."""


class ReceiptHistoryUnavailable(ValueError):
    """Required immutable historical data is unavailable, not contradicted."""


def receipt_result(
    row: TreasuryVerifiedReceipt, *, replayed: bool
) -> TreasuryReceiptResult:
    return TreasuryReceiptResult(
        receipt_id=row.receipt_id,
        stage=cast(Literal["service_distribution", "vendor_payment"], row.stage),
        public_event_id=row.public_event_id,
        published=row.published,
        replayed=replayed,
        epoch_index=row.epoch_index,
        bucket_id=row.bucket_id,
        policy_digest=row.policy_digest,
        source_block=row.source_block,
        source_claims=row.proof.get("source_claims", []),
        block=row.proof["block"],
        block_hash=row.block_hash,
        extrinsic_index=row.extrinsic_index,
        extrinsic_hash=row.proof["extrinsic_hash"],
        amount_atomic=str(row.amount_atomic),
    )


async def ingest_receipt(
    session: AsyncSession, chain: Any, selector: TreasuryReceiptSelector
) -> TreasuryReceiptResult:
    result = await _receipt(session, chain, selector, record=True)
    assert isinstance(result, TreasuryReceiptResult)
    return result


async def preflight_receipt(
    session: AsyncSession, chain: Any, selector: TreasuryReceiptSelector
) -> TreasuryReceiptPreflight:
    result = await _receipt(session, chain, selector, record=False)
    assert isinstance(result, TreasuryReceiptPreflight)
    return result


async def _receipt(
    session: AsyncSession,
    chain: Any,
    selector: TreasuryReceiptSelector,
    *,
    record: bool,
    claim_amount: int | None = None,
    evidence_only: bool = False,
) -> TreasuryReceiptResult | TreasuryReceiptPreflight | dict[str, Any]:
    if selector.source_claims:
        from ditto.api_server.treasury_aggregate import aggregate_receipt

        return await aggregate_receipt(session, chain, selector, record=record)
    if selector.stage == "provider_credit":
        # No independently authenticated provider reconciliation contract exists.
        # An administrator, journal entry or chain transfer cannot supply it.
        raise ValueError("provider credit verifier is not configured")
    # Serialize source claims and linked payments across processes. No UPDATE
    # changes the pin; the row lock only fences duplicate/aggregate consumption.
    ledger_query = select(LedgerEpochSnapshot).where(
        LedgerEpochSnapshot.netuid == 118,
        LedgerEpochSnapshot.epoch_index == selector.epoch_index,
    )
    ledger = await session.scalar(
        ledger_query.with_for_update() if record else ledger_query
    )
    if ledger is None:
        raise ReceiptHistoryUnavailable("historical immutable epoch pin absent")
    pin = treasury_pin_from_context(LedgerPin.from_row(ledger))
    if not isinstance(pin, EnforcingTreasuryPin):
        raise ValueError("historical approved enforcing epoch required")
    policy = verify_policy_approval(
        pin.approval,
        expected_policy_digest=pin.policy_digest,
        expected_collector_policy_digest=pin.policy.collector_policy_digest,
        verify_signature=verify_public_signature,
    )
    if selector.source_block is not None and selector.source_block < pin.first_block:
        raise ValueError("source earning precedes pinned epoch")
    if selector.block < pin.pinned_block:
        raise ValueError("payment precedes historical approved epoch")
    # Offline approvals and operator settings have independent revision counters.
    # Bind publication/payee rules to the latest append-only settings recorded
    # by the immutable epoch pin's time, never today's settings or a numerically
    # matching offline revision. Validate that row below; do not search backward
    # for an older row that happens to match the signed destinations.
    revision = await session.scalar(
        select(TreasurySettingsRevision)
        .where(TreasurySettingsRevision.created_at <= ledger.pinned_at)
        .order_by(TreasurySettingsRevision.revision.desc())
        .limit(1)
    )
    if revision is None or digest(revision.settings) != revision.checksum:
        raise ValueError("historical settings checksum invalid or absent")
    settings = TreasurySettings.model_validate(revision.settings)
    if not settings_match_policy(settings, policy):
        raise ValueError("historical destination settings differ from signed policy")
    buckets = [b for b in settings.service_buckets if b.bucket_id == selector.bucket_id]
    # The epoch pin, not the settings row, records the allocation it folded.
    allocation = {b.bucket_id: b.allocation_bps for b in pin.effective_buckets}
    if len(buckets) != 1 or (
        selector.stage == "service_distribution"
        and not allocation[buckets[0].bucket_id]
    ):
        raise ValueError("active historical service bucket absent")
    bucket = buckets[0]
    parent = None
    if selector.stage == "service_distribution":
        sender, recipient, asset, hotkey = (
            policy.collector_coldkey,
            bucket.holding_coldkey,
            "SN118_ALPHA",
            policy.collector_hotkey,
        )
    else:
        if selector.parent_receipt_id is not None:
            parent = await session.get(
                TreasuryVerifiedReceipt, selector.parent_receipt_id
            )
            if (
                parent is None
                or parent.stage != "service_distribution"
                or (
                    parent.epoch_index,
                    parent.bucket_id,
                    parent.source_block,
                    parent.policy_digest,
                )
                != (
                    selector.epoch_index,
                    selector.bucket_id,
                    selector.source_block,
                    policy.digest,
                )
                or selector.block < parent.proof["block"]
            ):
                raise ValueError("payment lacks same historical verified distribution")
        rules = [
            r
            for r in bucket.payee_rules
            if r.enabled and r.rule_id == selector.payee_rule_id
        ]
        if len(rules) != 1:
            raise ValueError("unique historical payee rule absent")
        rule = rules[0]
        sender, recipient, asset, hotkey = (
            bucket.holding_coldkey,
            rule.recipient_coldkey,
            rule.asset,
            rule.recipient_hotkey,
        )
        if asset != "TAO" and parent is None:
            raise ValueError("alpha lineage requires a verified source distribution")
    if recipient is None:
        raise ValueError("historical destination absent")
    # The production chain adapter independently reads finality/runtime and
    # actual liquid earning/effect. This never calls a signing/submit method.
    proof = await chain.get_treasury_receipt_proof(
        selector,
        policy,
        sender=sender,
        recipient=recipient,
        asset=asset,
        recipient_hotkey=hotkey,
        pinned_block=pin.pinned_block,
        pinned_block_hash=pin.pinned_block_hash,
        pinned_uid=pin.identity.uid,
    )
    recorded_policy_at = revision.created_at
    if recorded_policy_at.tzinfo is None:
        recorded_policy_at = recorded_policy_at.replace(tzinfo=UTC)
    if proof.event_at < recorded_policy_at:
        raise ValueError("payment precedes recorded historical destination policy")
    public_proof = asdict(proof)
    public_proof["event_at"] = proof.event_at.isoformat()
    public_proof["block"] = selector.block
    public_proof["source_block"] = selector.source_block
    if parent is not None:
        parent_source = parent.proof
        if parent.proof.get("source_claims"):
            matches = [
                c
                for c in parent.proof["source_claims"]
                if c["source_block"] == selector.source_block
            ]
            if len(matches) != 1:
                raise ReceiptConflict("parent aggregate source is ambiguous")
            parent_source = matches[0]
        if tuple(
            parent_source[k]
            for k in ("source_block_hash", "source_event_digest", "source_amount_rao")
        ) != (
            proof.source_block_hash,
            proof.source_event_digest,
            proof.source_amount_rao,
        ):
            raise ReceiptConflict("source credit differs from historical distribution")
    request_digest = digest(
        {
            "selector": selector.model_dump(
                mode="json", exclude={"reason", "source_claims"}
            ),
            "settings_checksum": revision.checksum,
            "policy_digest": policy.digest,
            "proof": public_proof,
        }
    )
    receipt_id = digest(
        {
            "chain": policy.genesis_hash,
            "block_hash": proof.block_hash,
            "extrinsic_index": selector.extrinsic_index,
            "event_index": proof.event_index,
        }
    )
    existing = await session.get(TreasuryVerifiedReceipt, receipt_id)
    if existing is not None and not evidence_only:
        if existing.request_digest != request_digest:
            raise ReceiptConflict(
                "chain effect already recorded with different proof or policy"
            )
        if not record:
            return TreasuryReceiptPreflight(
                ready=True, receipt_id=receipt_id, already_recorded=True
            )
        return receipt_result(existing, replayed=True)
    if selector.stage == "service_distribution":
        prior = list(
            await session.scalars(
                select(TreasuryVerifiedReceipt).where(
                    TreasuryVerifiedReceipt.stage == "service_distribution",
                    TreasuryVerifiedReceipt.bucket_id == bucket.bucket_id,
                )
            )
        )
        split = plan_service_distribution(
            attributed_alpha_rao=proof.source_amount_rao,
            available_alpha_rao=proof.source_amount_rao,
            collector_coldkey=policy.collector_coldkey,
            destinations=tuple(
                ServiceDestination(b.bucket_id, b.allocation_bps, b.holding_coldkey)
                for b in pin.effective_buckets
            ),
        )
        from ditto.api_server.treasury_aggregate import source_consumption

        consumed = source_consumption(
            prior,
            selector.source_block,
            policy.digest,
            revision.revision,
            proof,
            ignore_receipt=receipt_id if evidence_only else None,
        )
        entitlement = {item.bucket_id: item.alpha_rao for item in split}.get(
            bucket.bucket_id, 0
        )
        amount = proof.amount_atomic if claim_amount is None else claim_amount
        if not 0 < amount <= entitlement:
            raise ValueError("transfer exceeds exact attributed service bucket")
        if consumed + amount > entitlement:
            raise ReceiptConflict("cumulative distributions exceed source bucket")
    elif asset == "SN118_ALPHA":
        payments = list(
            await session.scalars(
                select(TreasuryVerifiedReceipt).where(
                    TreasuryVerifiedReceipt.parent_receipt_id
                    == selector.parent_receipt_id
                )
            )
        )
        consumed = sum(
            p.amount_atomic for p in payments if p.proof["asset"] == "SN118_ALPHA"
        )
        if parent is None or consumed + proof.amount_atomic > parent.amount_atomic:
            raise ReceiptConflict("linked alpha payments exceed finalized distribution")
    evidence = {
        "selector": selector,
        "proof": proof,
        "policy": policy,
        "revision": revision,
        "bucket": bucket,
        "allocation": allocation,
        "asset": asset,
        "public_proof": public_proof,
        "request_digest": request_digest,
        "receipt_id": receipt_id,
    }
    if evidence_only:
        return evidence
    if not record:
        return TreasuryReceiptPreflight(ready=True, receipt_id=receipt_id)
    return await save_receipt(session, **evidence)


async def save_receipt(
    session,
    *,
    selector,
    proof,
    policy,
    revision,
    bucket,
    allocation,
    asset,
    public_proof,
    request_digest,
    receipt_id,
):
    event = None
    if bucket.publish_payments:
        # Vendor transfers prove payment, not alpha conversion/funding lineage.
        # The distinct denominator and zero allocation prohibit that overclaim.
        distribution = selector.stage == "service_distribution"
        event = TreasuryPublicEvent(
            payment_id=receipt_id,
            bucket_id=bucket.bucket_id,
            policy_digest=policy.digest,
            epoch_index=selector.epoch_index,
            event_kind=selector.stage,
            state="chain_finalized",
            finalized_event_id=None,
            event_at=proof.event_at,
            policy_revision=policy.revision,
            burn_revision=0,
            burn_share_micros=0,
            denominator="collector_liquid_emission"
            if distribution
            else "not_attributed",
            maintenance_bps=0,
            gm_bps=0,
            allocation_bps=allocation[bucket.bucket_id] if distribution else 0,
            allocated_alpha_rao=proof.amount_atomic if distribution else 0,
            source_alpha_rao=proof.source_amount_rao if distribution else 0,
            route="alpha_transfer" if asset == "SN118_ALPHA" else "tao_transfer",
            deposit_asset=asset,
            deposit_amount_atomic=proof.amount_atomic,
            credited_usd_nano=None,
            bounty_award_id=None,
            accepted_work_ref=None,
            public_sender=proof.sender,
            public_recipient=proof.recipient,
            block_hash=proof.block_hash,
            extrinsic_index=selector.extrinsic_index,
            event_index=proof.event_index,
            actor_provenance="treasury_observer",
            actor_public_id="receipt-ingress-v1",
            verification_source="finalized_chain_rpc",
        )
        session.add(event)
        await session.flush()
    row = TreasuryVerifiedReceipt(
        receipt_id=receipt_id,
        request_digest=request_digest,
        stage=selector.stage,
        parent_receipt_id=selector.parent_receipt_id,
        epoch_index=selector.epoch_index,
        settings_revision=revision.revision,
        policy_digest=policy.digest,
        collector_policy_digest=policy.collector_policy_digest,
        bucket_id=bucket.bucket_id,
        source_block=selector.source_block,
        block_hash=proof.block_hash,
        extrinsic_index=selector.extrinsic_index,
        event_index=proof.event_index,
        amount_atomic=proof.amount_atomic,
        proof=public_proof,
        published=event is not None,
        public_event_id=event.id if event is not None else None,
        reason=selector.reason.strip(),
        actor="platform_admin_token",
    )
    session.add(row)
    await session.flush()
    return receipt_result(row, replayed=False)
