"""Diagnostic receipts. Never write Score, agent lifecycle or rollout state."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator import ScoreReport
from ditto.db.models import BenchmarkCanary, ValidatorTicket
from ditto.db.queries.audit import append_audit_entry
from ditto.db.queries.inference import revoke_ticket_inference


async def canary_for_lease(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    deadline: datetime,
    for_update: bool = True,
) -> BenchmarkCanary | None:
    statement = select(BenchmarkCanary).where(
        BenchmarkCanary.agent_id == agent_id,
        BenchmarkCanary.validator_hotkey == validator_hotkey,
        BenchmarkCanary.deadline == deadline,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def finish_canary(
    session: AsyncSession,
    *,
    canary: BenchmarkCanary,
    ticket: ValidatorTicket,
    now: datetime,
    report: ScoreReport | None = None,
    signature: str | None = None,
    failure: str | None = None,
) -> None:
    """Consume exactly one diagnostic lease. No retries, billing or score writes."""
    if (
        ticket.purpose != TicketPurpose.BENCHMARK_CANARY
        or ticket.agent_id != canary.agent_id
        or ticket.validator_hotkey != canary.validator_hotkey
        or ticket.deadline != canary.deadline
        or ticket.bench_version != canary.bench_version
    ):
        raise HTTPException(409, "canary lease identity changed")
    result = report.model_dump(mode="json") if report is not None else None
    if canary.status == "completed" and result == canary.result:
        return  # Exact signed transport retry, never a second evaluation.
    if (
        canary.status != "issued"
        or ticket.status != TicketStatus.ISSUED
        or canary.deadline.replace(tzinfo=UTC) <= now
    ):
        raise HTTPException(409, "canary lease is terminal or expired")
    if report is not None and (
        report.bench_version != canary.bench_version
        or report.seed != canary.seed
        or (report.details or {}).get("dataset_sha256") != canary.dataset_sha256
    ):
        raise HTTPException(409, "canary report does not match the pinned dataset")
    await revoke_ticket_inference(session, ticket=ticket, now=now)
    canary.status = "completed" if report is not None else "failed"
    canary.finished_at = now
    canary.result = result
    canary.signature = signature
    canary.failure_detail = failure
    # A diagnostic is never SCORED in the ordinary ticket ledger. Its failure
    # is stored above, not in miner-fault/retry-budget fields.
    ticket.status = TicketStatus.EXPIRED
    ticket.retry_after = None
    ticket.failure_reason = None
    ticket.failure_detail = None
    ticket.failed_at = None
    ticket.provider_outage_epoch = None
    await append_audit_entry(
        session,
        agent_id=canary.agent_id,
        validator_hotkey=canary.validator_hotkey,
        event=f"benchmark_canary_{canary.status}",
        recorded_at=now,
        payload={
            "canary_id": str(canary.canary_id),
            "authoritative": False,
            "run_id": report.run_id if report is not None else None,
        },
    )
    await session.flush()
