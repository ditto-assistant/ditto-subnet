"""Durable Backroom manual requests; custody and receipt proof remain separate."""

import time
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_ingress import TreasuryReceiptSelector
from ditto.api_server.treasury_ingress import ingest_receipt
from ditto.api_server.treasury_runtime import lock_runtime, treasury_runtime
from ditto.db.models import TreasuryManualBridgeState as BridgeState
from ditto.db.models import TreasuryManualTransfer as Transfer
from ditto_screening_protocol.treasury_manual import (
    ManualEnvelope,
    ManualReadiness,
    ManualReport,
    ManualRequest,
)

ACTIVE = ("queued", "dispatched", "pending", "audit_pending")


async def lock_manual(session):
    await session.execute(text("SELECT pg_advisory_xact_lock(118,2749)"))


def transfer_result(row):
    return {
        "request_id": row.request_id,
        "envelope": row.envelope,
        "status": row.status,
        "actor": row.actor,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_error": row.last_error,
        "receipt": row.receipt,
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
                report.readiness
                and 0 <= int(time.time()) - report.observed_at <= 180
                and report.collector_policy_digest
                == runtime.treasury_approved_collector_policy_digest
                and report.readiness.policy == report.collector_policy_digest
            ):
                readiness = report.readiness
                blocked = None
    if not runtime.treasury_weight_enforcement:
        blocked = "Gamma is paused"
    if await session.scalar(
        select(Transfer.request_id).where(Transfer.status.in_(ACTIVE)).limit(1)
    ):
        blocked = "A previous transfer or its public receipt is still pending"
    if readiness and any(
        r.status == "published"
        and readiness.after_operation <= r.envelope["request"]["after_operation"]
        for r in rows
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


async def validate_envelope(session, config, envelope, *, enabled):
    view = await state(session, config, enabled=enabled)
    if view["blocked_reason"]:
        raise ValueError(view["blocked_reason"])
    readiness = ManualReadiness.model_validate(view["readiness"])
    req = envelope.request
    destinations = view["destinations"]
    if envelope.collector_policy_digest != readiness.policy or not any(
        b["bucket_id"] == req.bucket_id
        and b["allocation_bps"] > 0
        and b["holding_coldkey"] == envelope.destination
        for b in destinations
    ):
        raise ValueError("Destination differs from the approved signed policy")
    if (
        req.after_operation != readiness.after_operation
        or req.amount_rao > readiness.max_distribution_rao
        or req.amount_rao + req.retained_alpha_rao > readiness.available_alpha_rao
        or not readiness.finalized_block
        < req.expires_block
        <= readiness.finalized_block + 720
    ):
        raise ValueError(
            "Amount, retained reserve or custody revision changed; preview again"
        )
    if not any(
        s.source_block == req.source_block
        and any(
            p.bucket_id == req.bucket_id
            and p.holding_coldkey == envelope.destination
            and p.alpha_rao >= req.amount_rao
            for p in s.remaining
        )
        for s in readiness.sources
    ):
        raise ValueError("Amount exceeds a mature source's remaining entitlement")


async def preview(session, config, payload, *, enabled):
    view = await state(session, config, enabled=enabled)
    if view["blocked_reason"]:
        raise ValueError(view["blocked_reason"])
    readiness = ManualReadiness.model_validate(view["readiness"])
    matches = [
        (s.source_block, p.holding_coldkey)
        for s in readiness.sources
        for p in s.remaining
        if p.bucket_id == payload.bucket_id and p.alpha_rao >= payload.amount_rao
    ]
    if not matches:
        raise ValueError("No mature approved source can fund this amount")
    envelope = ManualEnvelope(
        collector_policy_digest=readiness.policy,
        destination=matches[0][1],
        request=ManualRequest(
            request_id=payload.request_id,
            after_operation=readiness.after_operation,
            source_block=matches[0][0],
            bucket_id=payload.bucket_id,
            amount_rao=payload.amount_rao,
            retained_alpha_rao=payload.retained_alpha_rao,
            expires_block=readiness.finalized_block + 600,
            reason=payload.reason.strip(),
        ),
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


async def accept_report(session, config, body):
    report = ManualReport.model_validate(body)
    runtime = await treasury_runtime(session, config)
    await lock_manual(session)
    if report.status == "readiness":
        if (
            report.collector_policy_digest
            != runtime.treasury_approved_collector_policy_digest
        ):
            raise ValueError("Custody readiness policy differs")
        if (
            not report.readiness
            or report.readiness.policy != report.collector_policy_digest
        ):
            raise ValueError("Custody readiness pin differs")
        now = int(time.time())
        if not 0 <= now - report.observed_at <= 180:
            return
        current = await session.get(BridgeState, 1)
        if current and current.report["observed_at"] >= report.observed_at:
            return
        if current is None:
            current = BridgeState(id=1)
            session.add(current)
        current.report, current.received_at = report.model_dump(), datetime.now(UTC)
        return
    row = await session.get(Transfer, report.request_id)
    if (
        row is None
        or row.digest != report.request_digest
        or row.envelope["collector_policy_digest"] != report.collector_policy_digest
    ):
        raise ValueError("Unknown or changed custody request")
    if row.status == "published":
        if report.status != "finalized" or row.report != report.model_dump():
            # observation timestamp alone is not part of immutable settlement.
            prior = ManualReport.model_validate(row.report)
            if prior.settlement != report.settlement or report.status != "finalized":
                raise ValueError("Published settlement changed")
        return
    if row.report:
        prior = ManualReport.model_validate(row.report)
        if prior.status in {"finalized", "failed", "refused"}:
            if prior.status != report.status or prior.settlement != report.settlement:
                raise ValueError("Terminal custody result changed")
            return
    if report.status == "finalized" and report.settlement is None:
        raise ValueError("Finalized coordinates absent")
    row.report = report.model_dump()
    row.status = "audit_pending" if report.status == "finalized" else report.status
    row.updated_at = datetime.now(UTC)


async def publish_audit(session: AsyncSession, chain, row):
    envelope = ManualEnvelope.model_validate(row.envelope)
    report = ManualReport.model_validate(row.report)
    if report.status != "finalized" or report.settlement is None:
        raise ValueError("Finalized report required for independent audit")
    req, proof = envelope.request, report.settlement
    result = await ingest_receipt(
        session,
        chain,
        TreasuryReceiptSelector(
            stage="service_distribution",
            bucket_id=req.bucket_id,
            source_block=req.source_block,
            amount_atomic=req.amount_rao,
            reason=req.reason,
            **proof.model_dump(),
        ),
    )
    if not result.published:
        raise ValueError("Signed source policy does not publish this receipt")
    row.receipt = result.model_dump(mode="json")
    row.status, row.last_error, row.updated_at = "published", None, datetime.now(UTC)
