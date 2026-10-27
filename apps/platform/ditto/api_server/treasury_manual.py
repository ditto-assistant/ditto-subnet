"""Durable Backroom manual requests; custody and receipt proof remain separate."""

import time
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import JSON, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_ingress import (
    TreasuryReceiptSelector,
    TreasurySourceClaim,
)
from ditto.api_server.treasury_ingress import ingest_receipt
from ditto.api_server.treasury_manual_movement import verified_movement_payload
from ditto.api_server.treasury_runtime import lock_runtime, treasury_runtime
from ditto.db.models import TreasuryManualBridgeState as BridgeState
from ditto.db.models import TreasuryManualTransfer as Transfer
from ditto_screening_protocol.treasury_manual import (
    ManualBatchEnvelope,
    ManualBatchRequest,
    ManualEnvelope,
    ManualReadiness,
    ManualReport,
    ManualRequest,
    parse_manual_envelope,
)

# Requests whose custody delivery is still unresolved. audit_pending already
# carries a finalized custody report; its delivery is resolved only once the
# wallet movement is independently verified (see delivery_verified).
UNSETTLED = ("queued", "dispatched", "pending")
OBSERVATION_BLOCKERS = {
    "runtime_changed": (
        "Finney runtime changed; custody audit and offline approval required"
    ),
    "policy_runtime_unreviewed": (
        "Custody policy requires a renewed offline runtime approval"
    ),
    "observation_unavailable": "Custody cannot verify its current observation",
}


def readiness_is_fresh(observed_at):
    # Independent hosts can differ by a few seconds; this is observation
    # freshness only. Custody still rechecks the finalized chain and exact claim.
    return -5 <= int(time.time()) - observed_at <= 180


async def lock_manual(session):
    await session.execute(text("SELECT pg_advisory_xact_lock(118,2749)"))


def delivery_verified(row) -> bool:
    """True only for an exact, independently decoded finalized wallet movement.

    Source attribution may remain incomplete forever (absent immutable epoch
    snapshots are never reconstructed). That is a publication gap, not delivery
    ambiguity: the movement proof binds this request's finalized coordinates,
    destination and amount, and custody's own journal fences source reuse.
    """
    if row.status != "audit_pending" or row.receipt is None:
        return False
    try:
        movement = verified_movement_payload(row.receipt)
        envelope = parse_manual_envelope(row.envelope)
        report = ManualReport.model_validate(row.report)
    except ValueError:
        return False
    settlement = report.settlement
    return (
        report.status == "finalized"
        and settlement is not None
        and row.digest == envelope.digest == report.request_digest
        and report.request_id == envelope.request.request_id == row.request_id
        and movement.extrinsic_hash == settlement.extrinsic_hash
        and movement.block_hash == settlement.block_hash
        and movement.block == settlement.block
        and movement.extrinsic_index == settlement.extrinsic_index
        and movement.public_recipient == envelope.destination
        and movement.deposit_amount_atomic == str(envelope.request.amount_rao)
        and movement.bucket_id == envelope.request.bucket_id
    )


def transfer_result(row):
    report = ManualReport.model_validate(row.report) if row.report else None
    return {
        "request_id": row.request_id,
        "envelope": row.envelope,
        "status": row.status,
        "actor": row.actor,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_error": row.last_error,
        "receipt": row.receipt,
        "custody_result": {
            "status": report.status,
            "observed_at": report.observed_at,
            "failure_code": report.failure_code,
            "settlement": report.settlement.model_dump() if report.settlement else None,
        }
        if report
        else None,
    }


async def state(session, config, *, enabled):
    runtime = await treasury_runtime(session, config)
    bridge = await session.get(BridgeState, 1)
    rows = list(
        await session.scalars(
            select(Transfer).order_by(Transfer.created_at.desc()).limit(20)
        )
    )
    readiness: ManualReadiness | None = None
    blocked: str | None = "Manual custody bridge is disabled"
    if enabled:
        blocked = "Waiting for a fresh custody observation"
        if bridge:
            report = ManualReport.model_validate(bridge.report)
            if (
                report.status == "blocked"
                and report.blocker
                and readiness_is_fresh(report.observed_at)
                and report.collector_policy_digest
                == runtime.treasury_approved_collector_policy_digest
            ):
                blocked = OBSERVATION_BLOCKERS[report.blocker.code]
            if (
                report.readiness
                and readiness_is_fresh(report.observed_at)
                and report.collector_policy_digest
                == runtime.treasury_approved_collector_policy_digest
                and report.readiness.policy == report.collector_policy_digest
            ):
                readiness = report.readiness
                blocked = None
    if enabled and not runtime.treasury_weight_enforcement:
        blocked = "Gamma is paused"
    audited = list(
        await session.scalars(
            select(Transfer).where(Transfer.status == "audit_pending")
        )
    )
    delivered = [r for r in audited if delivery_verified(r)]
    if enabled and len(delivered) != len(audited):
        blocked = "A previous transfer or its public receipt is still pending"
    if enabled and await session.scalar(
        select(Transfer.request_id)
        .where(
            Transfer.status.in_(UNSETTLED)
            | (
                (
                    (Transfer.status == "failed")
                    | (
                        (Transfer.status == "refused")
                        & Transfer.dispatch_attempted_at.is_not(None)
                    )
                )
                & (Transfer.report.is_(None) | (Transfer.report == JSON.NULL))
            )
        )
        .limit(1)
    ):
        blocked = "A previous transfer or its public receipt is still pending"
    if readiness and (
        await session.scalar(
            select(Transfer.request_id)
            .where(
                Transfer.status == "published",
                Transfer.envelope["request"]["after_operation"].as_integer()
                >= readiness.after_operation,
            )
            .limit(1)
        )
        or any(
            r.envelope["request"]["after_operation"] >= readiness.after_operation
            for r in delivered
        )
    ):
        blocked = "Waiting for custody to observe the completed claim"
    if readiness and not readiness.bounded_claim_available:
        blocked = "Custody cannot arm another claim until prior delivery is resolved"
    return {
        "enabled": enabled,
        "blocked_reason": blocked,
        "readiness": readiness.model_dump() if readiness else None,
        "destinations": [b.model_dump() for b in runtime.treasury_shadow_policy.buckets]
        if runtime.treasury_shadow_policy
        else [],
        "requests": [transfer_result(r) for r in rows],
        "recurring_enabled": False,
    }


def _fundable(bucket: dict) -> bool:
    # A ranged bucket may receive emission whenever its signed ceiling is
    # positive, even if today's in-range Gamma % (or its default) is zero.
    return bucket.get("max_allocation_bps", bucket["allocation_bps"]) > 0


async def validate_envelope(session, config, envelope, *, enabled):
    view = await state(session, config, enabled=enabled)
    if view["blocked_reason"]:
        raise ValueError(view["blocked_reason"])
    readiness = ManualReadiness.model_validate(view["readiness"])
    req = envelope.request
    destinations = view["destinations"]
    if envelope.collector_policy_digest != readiness.policy or not any(
        b["bucket_id"] == req.bucket_id
        and _fundable(b)
        and b["holding_coldkey"] == envelope.destination
        for b in destinations
    ):
        raise ValueError("Destination differs from the approved signed policy")
    if (
        req.after_operation != readiness.after_operation
        or req.amount_rao > effective_manual_limit(readiness, envelope)
        or req.amount_rao + req.retained_alpha_rao > readiness.available_alpha_rao
        or not readiness.finalized_block
        < req.expires_block
        <= readiness.finalized_block + 720
    ):
        raise ValueError(
            "Amount, retained reserve or custody revision changed; preview again"
        )
    claims = (
        req.sources
        if isinstance(envelope, ManualBatchEnvelope)
        else [SimpleNamespace(source_block=req.source_block, amount_rao=req.amount_rao)]
    )
    for claim in claims:
        if not any(
            s.source_block == claim.source_block
            and any(
                p.bucket_id == req.bucket_id
                and p.holding_coldkey == envelope.destination
                and p.alpha_rao >= claim.amount_rao
                for p in s.remaining
            )
            for s in readiness.sources
        ):
            raise ValueError("Amount exceeds mature remaining source entitlement")


def effective_manual_limit(readiness, envelope=None):
    approval = readiness.manual_approval
    if approval is not None:
        # Custody's observation cannot forge cold approval. The live runtime's
        # collector identity is checked when accepting the report below.
        approval.verify(readiness.policy, approval.policy.collector_coldkey)
        if envelope is not None and (
            not isinstance(envelope, ManualBatchEnvelope)
            or envelope.manual_approval != approval
        ):
            raise ValueError("Manual approval changed; preview again")
        return approval.policy.max_amount_rao
    if isinstance(envelope, ManualBatchEnvelope):
        raise ValueError("Aggregate manual authority is not deployed")
    return readiness.max_distribution_rao


async def preview(session, config, payload, *, enabled):
    view = await state(session, config, enabled=enabled)
    if view["blocked_reason"]:
        raise ValueError(view["blocked_reason"])
    readiness = ManualReadiness.model_validate(view["readiness"])
    approved = {
        (b["bucket_id"], b["holding_coldkey"])
        for b in view["destinations"]
        if _fundable(b)
    }
    matches = [
        (s.source_block, p.holding_coldkey, p.alpha_rao)
        for s in readiness.sources
        for p in s.remaining
        if p.bucket_id == payload.bucket_id
        and (p.bucket_id, p.holding_coldkey) in approved
    ]
    base = {
        "request_id": payload.request_id,
        "after_operation": readiness.after_operation,
        "bucket_id": payload.bucket_id,
        "amount_rao": payload.amount_rao,
        "retained_alpha_rao": payload.retained_alpha_rao,
        "expires_block": readiness.finalized_block + 600,
        "reason": payload.reason.strip(),
    }
    if readiness.manual_approval:
        if payload.amount_rao > effective_manual_limit(readiness):
            raise ValueError("Amount exceeds cold-approved manual ceiling")
        claims, left = [], payload.amount_rao
        for block, _destination, amount in sorted(matches):
            take = min(left, amount)
            if take:
                claims.append({"source_block": block, "amount_rao": take})
                left -= take
            if not left:
                break
        if left or not claims:
            raise ValueError("Verified mature earnings cannot fund this amount yet")
        envelope = ManualBatchEnvelope(
            collector_policy_digest=readiness.policy,
            destination=matches[0][1],
            manual_approval=readiness.manual_approval,
            request=ManualBatchRequest(
                **base,
                source_block=claims[0]["source_block"],
                sources=claims,
            ),
        )
    else:
        matches = [m for m in matches if m[2] >= payload.amount_rao]
        if not matches:
            raise ValueError("No mature approved source can fund this amount")
        envelope = ManualEnvelope(
            collector_policy_digest=readiness.policy,
            destination=matches[0][1],
            request=ManualRequest(**base, source_block=matches[0][0]),
        )
    await validate_envelope(session, config, envelope, enabled=enabled)
    return {
        "envelope": envelope.model_dump(),
        "confirmation_digest": envelope.digest,
        "spending_authority": "not_queued",
    }


async def submit(session, config, payload, actor, *, enabled):
    await lock_runtime(session)
    await lock_manual(session)
    envelope = payload.envelope
    if envelope.digest != payload.confirmation_digest:
        raise ValueError("Exact preview confirmation required")
    existing = await session.get(Transfer, envelope.request.request_id)
    if existing:
        if existing.digest != envelope.digest or existing.actor != actor:
            raise ValueError("Request UUID was already used with different details")
        return transfer_result(existing)
    await validate_envelope(session, config, envelope, enabled=enabled)
    row = Transfer(
        request_id=envelope.request.request_id,
        envelope=envelope.model_dump(),
        digest=envelope.digest,
        actor=actor,
        status="queued",
    )
    session.add(row)
    await session.flush()
    return transfer_result(row)


class InvalidManualReport(ValueError):
    """Permanent wire/provenance refusal; never a chain or database outage."""


async def accept_report(session, config, body):
    from pydantic import ValidationError

    try:
        report = ManualReport.model_validate(body)
    except ValidationError as error:
        raise InvalidManualReport("Invalid custody report contract") from error
    await lock_runtime(session)
    runtime = await treasury_runtime(session, config)
    await lock_manual(session)
    if report.status in {"readiness", "blocked"}:
        if (
            report.collector_policy_digest
            != runtime.treasury_approved_collector_policy_digest
        ):
            raise InvalidManualReport("Custody readiness policy differs")
        if report.status == "readiness" and (
            not report.readiness
            or report.readiness.policy != report.collector_policy_digest
        ):
            raise InvalidManualReport("Custody readiness pin differs")
        if report.readiness and report.readiness.manual_approval:
            if runtime.treasury_shadow_policy is None:
                raise InvalidManualReport("Approved collector identity absent")
            try:
                report.readiness.manual_approval.verify(
                    report.collector_policy_digest,
                    runtime.treasury_shadow_policy.collector_coldkey,
                )
            except ValueError as error:
                raise InvalidManualReport("Manual range lacks cold approval") from error
        if not readiness_is_fresh(report.observed_at):
            return
        current = await session.get(BridgeState, 1)
        if current and current.report["observed_at"] >= report.observed_at:
            return
        if current is None:
            current = BridgeState(id=1)
            session.add(current)
        current.report, current.received_at = report.model_dump(), datetime.now(UTC)
        return
    # A result belongs to its immutable dispatched request, not today's
    # runtime policy. Rotation must not discard an older claim's settlement.
    row = await session.get(Transfer, report.request_id)
    if (
        row is None
        or row.digest != report.request_digest
        or row.envelope["collector_policy_digest"] != report.collector_policy_digest
    ):
        raise InvalidManualReport("Unknown or changed custody request")
    if row.status == "published":
        if report.status != "finalized" or row.report != report.model_dump():
            # observation timestamp alone is not part of immutable settlement.
            prior = ManualReport.model_validate(row.report)
            if prior.settlement != report.settlement or report.status != "finalized":
                raise InvalidManualReport("Published settlement changed")
        return
    if row.report:
        prior = ManualReport.model_validate(row.report)
        if prior.status in {"finalized", "failed", "refused"}:
            if prior.status != report.status or prior.settlement != report.settlement:
                raise InvalidManualReport("Terminal custody result changed")
            return
    if report.status == "finalized" and report.settlement is None:
        raise InvalidManualReport("Finalized coordinates absent")
    # A local dispatch refusal without a custody report is not proof of no
    # delivery: publication may have succeeded before its acknowledgment was
    # lost. Preserve a late settlement for independent chain audit, never resend.
    row.report = report.model_dump()
    row.status = "audit_pending" if report.status == "finalized" else report.status
    row.updated_at = datetime.now(UTC)


async def publish_audit(session: AsyncSession, chain, row):
    envelope = parse_manual_envelope(row.envelope)
    report = ManualReport.model_validate(row.report)
    if report.status != "finalized" or report.settlement is None:
        raise ValueError("Finalized report required for independent audit")
    req, proof = envelope.request, report.settlement
    source_claims = []
    if isinstance(envelope, ManualBatchEnvelope):
        epochs = {s.source_block: s.epoch_index for s in proof.source_epochs}
        if (
            len(epochs) != len(proof.source_epochs)
            or set(epochs) != {s.source_block for s in req.sources}
            or epochs[req.source_block] != proof.epoch_index
        ):
            raise ValueError("Finalized aggregate source epochs differ from request")
        source_claims = [
            TreasurySourceClaim(**s.model_dump(), epoch_index=epochs[s.source_block])
            for s in req.sources
        ]
    result = await ingest_receipt(
        session,
        chain,
        TreasuryReceiptSelector(
            stage="service_distribution",
            bucket_id=req.bucket_id,
            source_block=req.source_block,
            source_claims=source_claims,
            amount_atomic=req.amount_rao,
            reason=req.reason,
            **proof.model_dump(),
        ),
    )
    if not result.published:
        raise ValueError("Signed source policy does not publish this receipt")
    row.receipt = result.model_dump(mode="json")
    row.status, row.last_error, row.updated_at = "published", None, datetime.now(UTC)
