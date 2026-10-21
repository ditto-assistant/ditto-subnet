"""Publish an independently verified wallet movement without inventing epochs.

This does not ingest a source distribution, consume an earnings entitlement,
complete the allocation audit, ACK custody, or call any signing interface.
"""

from sqlalchemy import select, text

from ditto.api_models.treasury_movement import (
    PublicTreasuryMovement,
    TreasuryMovementSelector,
)
from ditto.api_models.treasury_settings import TreasurySettings
from ditto.api_server.ledger_pin import LedgerPin, treasury_pin_from_context
from ditto.api_server.treasury_ingress import ReceiptHistoryUnavailable, digest
from ditto.api_server.treasury_runtime import settings_match_policy
from ditto.db.models import (
    LedgerEpochSnapshot,
    TreasuryManualTransfer,
    TreasurySettingsRevision,
    TreasuryVerifiedReceipt,
)
from ditto_screening_protocol.treasury_approval import (
    verify_policy_approval,
    verify_public_signature,
)
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin
from ditto_screening_protocol.treasury_manual import (
    ManualBatchEnvelope,
    ManualReport,
    parse_manual_envelope,
)

KIND = "finalized_manual_movement_v1"


def verified_movement_payload(receipt):
    """Validate the entire retained projection, including non-financial fields."""
    if not isinstance(receipt, dict) or receipt.get("proof_kind") != KIND:
        raise ValueError("verified manual movement proof required")
    movement = PublicTreasuryMovement.model_validate(receipt.get("movement"))
    if (
        receipt.get("movement_digest") != digest(movement.model_dump(mode="json"))
        or receipt.get("receipt_id") != movement.payment_id
        or receipt.get("extrinsic_hash") != movement.extrinsic_hash
        or receipt.get("published") is not True
        or receipt.get("source_attribution") != "incomplete"
    ):
        raise ValueError("retained manual movement proof changed")
    return movement


async def publish_finalized_movement(session, chain, row):
    envelope = parse_manual_envelope(row.envelope)
    report = ManualReport.model_validate(row.report)
    req, settlement = envelope.request, report.settlement
    if (
        row.status != "audit_pending"
        or report.status != "finalized"
        or settlement is None
        or row.request_id != req.request_id
        or report.request_id != req.request_id
        or row.digest != envelope.digest
        or report.request_digest != envelope.digest
        or report.collector_policy_digest != envelope.collector_policy_digest
    ):
        raise ValueError("exact finalized manual request required")
    if row.receipt is not None:
        cached = verified_movement_payload(row.receipt)
        if (
            cached.extrinsic_hash != settlement.extrinsic_hash
            or cached.block_hash != settlement.block_hash
            or cached.block != settlement.block
            or cached.extrinsic_index != settlement.extrinsic_index
            or cached.public_recipient != envelope.destination
            or cached.deposit_amount_atomic != str(req.amount_rao)
            or cached.bucket_id != req.bucket_id
        ):
            raise ValueError("finalized manual movement changed")
        return
    # Use the request's explicit historical anchor, never today's settings or
    # a guessed/carried-forward epoch. Other missing source epochs remain absent.
    ledger = await session.scalar(
        select(LedgerEpochSnapshot).where(
            LedgerEpochSnapshot.netuid == 118,
            LedgerEpochSnapshot.epoch_index == settlement.epoch_index,
        )
    )
    if ledger is None:
        raise ReceiptHistoryUnavailable("manual wallet identity anchor absent")
    pin = treasury_pin_from_context(LedgerPin.from_row(ledger))
    if not isinstance(pin, EnforcingTreasuryPin):
        raise ValueError("historical approved wallet identity anchor required")
    policy = verify_policy_approval(
        pin.approval,
        expected_policy_digest=pin.policy_digest,
        expected_collector_policy_digest=pin.policy.collector_policy_digest,
        verify_signature=verify_public_signature,
    )
    revision = await session.scalar(
        select(TreasurySettingsRevision)
        .where(TreasurySettingsRevision.created_at <= ledger.pinned_at)
        .order_by(TreasurySettingsRevision.revision.desc())
        .limit(1)
    )
    if revision is None or digest(revision.settings) != revision.checksum:
        raise ValueError("historical publication settings invalid")
    settings = TreasurySettings.model_validate(revision.settings)
    if not settings_match_policy(settings, policy):
        raise ValueError("historical publication settings differ from signed policy")
    buckets = [b for b in settings.service_buckets if b.bucket_id == req.bucket_id]
    if (
        len(buckets) != 1
        or buckets[0].holding_coldkey != envelope.destination
        or not buckets[0].publish_payments
        or settlement.block < pin.pinned_block
    ):
        raise ValueError("historical wallet publication not authorized")
    if isinstance(envelope, ManualBatchEnvelope):
        approval = envelope.manual_approval.verify(
            envelope.collector_policy_digest, policy.collector_coldkey
        )
        if req.amount_rao > approval.max_amount_rao:
            raise ValueError("manual movement exceeds approved range")
    selector = TreasuryMovementSelector(
        block=settlement.block,
        block_hash=settlement.block_hash,
        extrinsic_index=settlement.extrinsic_index,
        extrinsic_hash=settlement.extrinsic_hash,
        amount_atomic=req.amount_rao,
    )
    # Same canonical runtime/origin/proxy/event decoder as full receipts, with
    # no source selector: this asserts the payment effect, NOT its attribution.
    proof = await chain.get_treasury_receipt_proof(
        selector,
        policy,
        sender=policy.collector_coldkey,
        recipient=envelope.destination,
        asset="SN118_ALPHA",
        recipient_hotkey=policy.collector_hotkey,
        pinned_block=pin.pinned_block,
        pinned_block_hash=pin.pinned_block_hash,
        pinned_uid=pin.identity.uid,
    )
    if (
        proof.block_hash != selector.block_hash
        or proof.extrinsic_hash != selector.extrinsic_hash
        or proof.sender != policy.collector_coldkey
        or proof.recipient != envelope.destination
        or proof.asset != "SN118_ALPHA"
        or proof.amount_atomic != req.amount_rao
        or proof.source_amount_rao != 0
        or proof.source_block_hash is not None
        or proof.source_event_digest is not None
    ):
        raise ValueError("canonical wallet movement differs from request")
    receipt_id = digest(
        {
            "chain": policy.genesis_hash,
            "block_hash": proof.block_hash,
            "extrinsic_index": selector.extrinsic_index,
            "event_index": proof.event_index,
        }
    )
    # Serialize the public projection's cross-request identity, independently
    # from the per-request SQL lock. This reserves NO source/spending balance.
    await session.execute(text("SELECT pg_advisory_xact_lock(118,2750)"))
    other = await session.scalar(
        select(TreasuryManualTransfer.request_id)
        .where(
            TreasuryManualTransfer.request_id != row.request_id,
            TreasuryManualTransfer.receipt["receipt_id"].as_string() == receipt_id,
        )
        .limit(1)
    )
    if other is not None or await session.get(TreasuryVerifiedReceipt, receipt_id):
        raise ValueError("canonical movement already has another receipt")
    movement = PublicTreasuryMovement(
        payment_id=receipt_id,
        bucket_id=req.bucket_id,
        event_at=proof.event_at,
        public_sender=proof.sender,
        public_recipient=proof.recipient,
        deposit_amount_atomic=str(proof.amount_atomic),
        block=selector.block,
        block_hash=proof.block_hash,
        extrinsic_index=selector.extrinsic_index,
        event_index=proof.event_index,
        extrinsic_hash=proof.extrinsic_hash,
    )
    row.receipt = {
        "receipt_id": receipt_id,
        "extrinsic_hash": proof.extrinsic_hash,
        "published": True,
        "proof_kind": KIND,
        "source_attribution": "incomplete",
        "movement": movement.model_dump(mode="json"),
        "movement_digest": digest(movement.model_dump(mode="json")),
    }
