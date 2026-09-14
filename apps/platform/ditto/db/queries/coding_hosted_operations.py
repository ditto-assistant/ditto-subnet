"""Operator cancellation and redacted lifecycle reads for hosted Coding assignments.

Cancellation is bounded to assignments whose attempt never started. It closes the
bound private task with the existing one-way ``aborted`` close and then appends
one immutable row; PostgreSQL refuses that row while a task is still open. It
never deletes or rewrites assignment or evidence rows. A started attempt owns
candidate processes, relays and grants that only the Platform worker can quiesce
and account for, so it is stopped by the worker's own abort path rather than by
an operator database write.

Each read runs in one REPEATABLE READ, READ ONLY snapshot and selects named
non-secret columns only: digests, timestamps, outcomes and accounting totals.
The assignment authority document, private selections, catalog indices, patch
digests, grading bindings, test counts, sealed-blob coordinates, settlement
documents, result bodies, grant identifiers and worker identifiers are never
loaded here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, cast, get_args
from uuid import UUID

from sqlalchemy import Select, case, func, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_hosted_assignment_admin import (
    MAX_HOSTED_REASON_LENGTH,
    HostedAssignmentState,
    HostedCloseReason,
    HostedTerminalOutcome,
)
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedAssignmentCancellation,
    CodingHostedAuthoringFinalization,
    CodingHostedAuthoringReservation,
    CodingHostedGradingClaim,
    CodingHostedInferenceGrant,
    CodingHostedInferenceRequest,
    CodingHostedPrivateTask,
    CodingHostedResultAcknowledgement,
    CodingHostedResultDelivery,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)
from ditto.db.queries.coding_hosted_private import _record_close, _task

MAX_LISTED_DELIVERIES = 20


class HostedCancellationError(ValueError):
    """Refused cancellation; carries no private assignment contents."""


class HostedAssignmentNotFoundError(LookupError):
    """No hosted assignment exists for the requested evaluation."""


@dataclass(frozen=True)
class HostedCancellation:
    row: CodingHostedAssignmentCancellation
    idempotent: bool
    private_task_closed: bool


def hosted_operation_state(
    *,
    started_at: datetime | None,
    admitted_at: datetime | None,
    expires_at: datetime,
    closed_at: datetime | None,
    close_reason: str | None,
    cancelled: bool,
    now: datetime,
) -> HostedAssignmentState:
    """One derivation for every operator view; expiry is never a stored mutation."""
    if cancelled:
        return "cancelled"
    if closed_at is not None:
        if close_reason not in get_args(HostedCloseReason):
            raise ValueError("hosted private task close reason is invalid")
        return cast(HostedCloseReason, close_reason)
    if expires_at <= now:
        return "expired"
    if started_at is not None:
        return "running"
    if admitted_at is not None:
        return "admitted"
    return "pending_admission"


async def cancel_hosted_assignment(
    session: AsyncSession,
    *,
    evaluation_id: UUID,
    expected_assignment_sha256: str,
    actor: str,
    reason: str,
) -> HostedCancellation:
    """Close the task, then append one cancellation, inside the caller's txn.

    Lock order is assignment -> task, the same access-removal order as
    ``close_hosted_private_task``. Release and agent locks are never taken, so
    cancellation cannot deadlock admission/start (release -> agent -> assignment)
    and still works after release retirement or artifact drift.
    """
    reason, actor = reason.strip(), actor.strip()
    if not 8 <= len(reason) <= MAX_HOSTED_REASON_LENGTH or not 1 <= len(actor) <= 120:
        raise HostedCancellationError("hosted cancellation audit is invalid")
    assignment = (
        await session.execute(
            select(
                CodingHostedAssignment.assignment_sha256,
                CodingHostedAssignment.admitted_at,
                CodingHostedAssignment.started_at,
            )
            .where(CodingHostedAssignment.evaluation_id == evaluation_id)
            .with_for_update()
        )
    ).one_or_none()
    if assignment is None:
        raise HostedAssignmentNotFoundError("hosted assignment does not exist")
    if assignment.assignment_sha256 != expected_assignment_sha256:
        raise HostedCancellationError(
            "hosted assignment digest differs; re-read before cancelling"
        )
    existing = await session.get(
        CodingHostedAssignmentCancellation, evaluation_id, populate_existing=True
    )
    if existing is not None:
        if existing.reason != reason or existing.actor != actor:
            raise HostedCancellationError(
                "hosted assignment was already cancelled with different audit"
            )
        # PostgreSQL already refused the row while a task was open; a replay
        # still re-closes defensively so it converges instead of trusting that.
        closed = await _close_open_task(session, evaluation_id)
        return HostedCancellation(existing, idempotent=True, private_task_closed=closed)
    if assignment.started_at is not None:
        raise HostedCancellationError(
            "hosted attempt already started; only its worker can abort it"
        )
    closed = await _close_open_task(session, evaluation_id)
    session.add(
        CodingHostedAssignmentCancellation(
            evaluation_id=evaluation_id,
            assignment_sha256=assignment.assignment_sha256,
            prior_state=(
                "pending_admission" if assignment.admitted_at is None else "admitted"
            ),
            reason=reason,
            actor=actor,
        )
    )
    await session.flush()
    row = await session.get(
        CodingHostedAssignmentCancellation, evaluation_id, populate_existing=True
    )
    if row is None:  # pragma: no cover - the flush above inserted it
        raise HostedCancellationError("hosted cancellation was not recorded")
    return HostedCancellation(row, idempotent=False, private_task_closed=closed)


async def _close_open_task(session: AsyncSession, evaluation_id: UUID) -> bool:
    task = await _task(session, evaluation_id)
    return task is not None and await _record_close(session, task, "aborted")


@dataclass(frozen=True)
class HostedAssignmentSummary:
    """One redacted lifecycle; attribute names match the admin wire model."""

    evaluation_id: UUID
    attempt_id: UUID
    release_row_id: UUID
    registration_sha256: str
    agent_id: UUID
    validator_hotkey: str
    artifact_sha256: str
    screened_image_sha256: str
    assignment_sha256: str
    state: HostedAssignmentState
    created_at: datetime
    expires_at: datetime
    admitted_at: datetime | None
    started_at: datetime | None
    cancelled_at: datetime | None
    closed_at: datetime | None
    close_reason: HostedCloseReason | None
    terminal_outcome: HostedTerminalOutcome | None
    acknowledged: bool
    registered_actor: str
    registered_reason: str


@dataclass(frozen=True)
class HostedCancellationRecord:
    assignment_sha256: str
    prior_state: str
    reason: str
    actor: str
    cancelled_at: datetime


@dataclass(frozen=True)
class HostedPrivateTaskStatus:
    bound_at: datetime
    selection_sha256: str
    frozen_at: datetime | None
    closed_at: datetime | None
    close_reason: HostedCloseReason | None


@dataclass(frozen=True)
class HostedTerminalStatus:
    outcome: HostedTerminalOutcome
    evidence_sha256: str
    reserved_at: datetime
    finalized_at: datetime | None


@dataclass(frozen=True)
class HostedInferenceTotals:
    policy_sha256: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    request_limit: int
    prompt_token_limit: int
    completion_token_limit: int
    cost_usd_micros_limit: int
    request_count: int
    reserved_count: int
    settled_count: int
    uncertain_count: int
    charged_prompt_tokens: int
    charged_completion_tokens: int
    charged_cost_usd_micros: int
    settled_prompt_tokens: int
    settled_completion_tokens: int
    settled_cost_usd_micros: int

    @property
    def verified(self) -> bool:
        return (
            self.revoked_at is not None
            and self.reserved_count == 0
            and self.uncertain_count == 0
        )


@dataclass(frozen=True)
class HostedDelivery:
    result_sha256: str
    delivered_at: datetime
    acknowledged_at: datetime | None


@dataclass(frozen=True)
class HostedAssignmentDetail(HostedAssignmentSummary):
    observed_at: datetime
    deadline_unix: int
    selection_sha256: str
    policy_sha256: str
    execution_profile_sha256: str
    grading_profile_sha256: str
    admission_request_sha256: str | None
    cancellation: HostedCancellationRecord | None
    private_task: HostedPrivateTaskStatus | None
    authoring_evidence_reserved_at: datetime | None
    authoring_evidence_finalized_at: datetime | None
    grading_claimed_at: datetime | None
    terminal: HostedTerminalStatus | None
    inference: HostedInferenceTotals | None
    delivery_count: int
    acknowledged_count: int
    deliveries: tuple[HostedDelivery, ...]

    @property
    def cancellable(self) -> bool:
        return self.started_at is None and self.cancellation is None

    @property
    def deliveries_truncated(self) -> bool:
        return self.delivery_count > len(self.deliveries)


_SUMMARY_FIELDS = tuple(
    field.name for field in fields(HostedAssignmentSummary) if field.name != "state"
)
_INFERENCE_TOTALS = (
    "request_count",
    "reserved_count",
    "settled_count",
    "uncertain_count",
    "charged_prompt_tokens",
    "charged_completion_tokens",
    "charged_cost_usd_micros",
    "settled_prompt_tokens",
    "settled_completion_tokens",
    "settled_cost_usd_micros",
)


async def _snapshot(session: AsyncSession) -> None:
    # A coherent observation and a database-enforced no-write boundary.
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    )


def _summary_query(*columns: Any) -> Select[Any]:
    assignment = CodingHostedAssignment
    acknowledged = (
        select(CodingHostedResultAcknowledgement.result_sha256)
        .join(
            CodingHostedResultDelivery,
            CodingHostedResultDelivery.result_sha256
            == CodingHostedResultAcknowledgement.result_sha256,
        )
        .where(CodingHostedResultDelivery.evaluation_id == assignment.evaluation_id)
        .exists()
    )
    return (
        select(
            assignment.evaluation_id,
            assignment.attempt_id,
            assignment.release_row_id,
            assignment.registration_sha256,
            assignment.agent_id,
            assignment.validator_hotkey,
            assignment.artifact_sha256,
            assignment.screened_image_sha256,
            assignment.assignment_sha256,
            assignment.created_at,
            assignment.expires_at,
            assignment.admitted_at,
            assignment.started_at,
            assignment.actor.label("registered_actor"),
            assignment.reason.label("registered_reason"),
            CodingHostedAssignmentCancellation.cancelled_at,
            CodingHostedPrivateTask.closed_at,
            CodingHostedPrivateTask.close_reason,
            CodingHostedTerminalReservation.identity["outcome"]
            .as_string()
            .label("terminal_outcome"),
            acknowledged.label("acknowledged"),
            *columns,
        )
        .select_from(assignment)
        .outerjoin(
            CodingHostedAssignmentCancellation,
            CodingHostedAssignmentCancellation.evaluation_id
            == assignment.evaluation_id,
        )
        .outerjoin(
            CodingHostedPrivateTask,
            CodingHostedPrivateTask.evaluation_id == assignment.evaluation_id,
        )
        .outerjoin(
            CodingHostedTerminalReservation,
            CodingHostedTerminalReservation.evaluation_id == assignment.evaluation_id,
        )
    )


def _summary_values(row: Mapping[Any, Any], now: datetime) -> dict[str, Any]:
    values = {name: row[name] for name in _SUMMARY_FIELDS}
    if values["terminal_outcome"] not in (None, *get_args(HostedTerminalOutcome)):
        raise ValueError("hosted terminal outcome is invalid")
    values["acknowledged"] = bool(values["acknowledged"])
    values["state"] = hosted_operation_state(
        started_at=row["started_at"],
        admitted_at=row["admitted_at"],
        expires_at=row["expires_at"],
        closed_at=row["closed_at"],
        close_reason=row["close_reason"],
        cancelled=row["cancelled_at"] is not None,
        now=now,
    )
    return values


async def list_hosted_assignment_summaries(
    session: AsyncSession, *, limit: int, offset: int
) -> tuple[list[HostedAssignmentSummary], int, datetime]:
    """Newest first with a stable tie-break; total and page share one snapshot."""
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("hosted assignment page is invalid")
    async with session.begin():
        await _snapshot(session)
        total, now = (
            await session.execute(
                select(func.count(), func.clock_timestamp()).select_from(
                    CodingHostedAssignment
                )
            )
        ).one()
        rows = (
            await session.execute(
                _summary_query()
                .order_by(
                    CodingHostedAssignment.created_at.desc(),
                    CodingHostedAssignment.evaluation_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).mappings()
        summaries = [
            HostedAssignmentSummary(**_summary_values(row, now)) for row in rows
        ]
    return summaries, int(total), now


def _detail_query(evaluation_id: UUID) -> Select[Any]:
    authority = CodingHostedAssignment.authority
    cancellation = CodingHostedAssignmentCancellation
    task = CodingHostedPrivateTask
    grant = CodingHostedInferenceGrant
    request = CodingHostedInferenceRequest
    settled = request.state == "settled"

    def charged(actual: Any, ceiling: Any) -> Any:
        # The ledger's conservative rule: a settled request charges its trusted
        # usage, a reserved or uncertain one keeps its full ceiling.
        return func.coalesce(func.sum(case((settled, actual), else_=ceiling)), 0)

    def settled_sum(actual: Any) -> Any:
        return func.coalesce(func.sum(actual).filter(settled), 0)

    # Aggregates without GROUP BY yield exactly one row, so both cross joins are
    # safe for an assignment with no grant or no delivery.
    inference = (
        select(
            func.count().label("request_count"),
            func.count().filter(request.state == "reserved").label("reserved_count"),
            func.count().filter(settled).label("settled_count"),
            func.count().filter(request.state == "uncertain").label("uncertain_count"),
            charged(request.prompt_tokens, request.prompt_ceiling).label(
                "charged_prompt_tokens"
            ),
            charged(request.completion_tokens, request.completion_ceiling).label(
                "charged_completion_tokens"
            ),
            charged(request.cost_usd_micros, request.cost_ceiling).label(
                "charged_cost_usd_micros"
            ),
            settled_sum(request.prompt_tokens).label("settled_prompt_tokens"),
            settled_sum(request.completion_tokens).label("settled_completion_tokens"),
            settled_sum(request.cost_usd_micros).label("settled_cost_usd_micros"),
        )
        .select_from(request)
        .join(grant, grant.grant_id == request.grant_id)
        .where(grant.evaluation_id == evaluation_id)
        .subquery("inference_totals")
    )
    deliveries = (
        select(
            func.count(CodingHostedResultDelivery.result_sha256).label(
                "delivery_count"
            ),
            func.count(CodingHostedResultAcknowledgement.result_sha256).label(
                "acknowledged_count"
            ),
        )
        .select_from(CodingHostedResultDelivery)
        .outerjoin(
            CodingHostedResultAcknowledgement,
            CodingHostedResultAcknowledgement.result_sha256
            == CodingHostedResultDelivery.result_sha256,
        )
        .where(CodingHostedResultDelivery.evaluation_id == evaluation_id)
        .subquery("delivery_totals")
    )
    return (
        _summary_query(
            func.clock_timestamp().label("observed_at"),
            authority["deadline_unix"].as_integer().label("deadline_unix"),
            authority["selection_sha256"].as_string().label("selection_sha256"),
            authority["policy_sha256"].as_string().label("policy_sha256"),
            authority["execution_profile_sha256"]
            .as_string()
            .label("execution_profile_sha256"),
            authority["grading_profile_sha256"]
            .as_string()
            .label("grading_profile_sha256"),
            CodingHostedAssignment.admission_request_sha256,
            cancellation.assignment_sha256.label("cancellation_assignment_sha256"),
            cancellation.prior_state.label("cancellation_prior_state"),
            cancellation.reason.label("cancellation_reason"),
            cancellation.actor.label("cancellation_actor"),
            task.created_at.label("task_bound_at"),
            task.selection_sha256.label("task_selection_sha256"),
            task.frozen_at.label("task_frozen_at"),
            CodingHostedAuthoringReservation.created_at.label(
                "authoring_evidence_reserved_at"
            ),
            CodingHostedAuthoringFinalization.verified_at.label(
                "authoring_evidence_finalized_at"
            ),
            CodingHostedGradingClaim.created_at.label("grading_claimed_at"),
            CodingHostedTerminalReservation.identity_sha256.label(
                "terminal_evidence_sha256"
            ),
            CodingHostedTerminalReservation.created_at.label("terminal_reserved_at"),
            CodingHostedTerminalFinalization.verified_at.label("terminal_finalized_at"),
            grant.policy_sha256.label("inference_policy_sha256"),
            grant.created_at.label("inference_issued_at"),
            grant.expires_at.label("inference_expires_at"),
            grant.revoked_at.label("inference_revoked_at"),
            grant.request_limit.label("inference_request_limit"),
            grant.prompt_limit.label("inference_prompt_token_limit"),
            grant.completion_limit.label("inference_completion_token_limit"),
            grant.cost_limit.label("inference_cost_usd_micros_limit"),
            *inference.c,
            *deliveries.c,
        )
        .outerjoin(
            CodingHostedAuthoringReservation,
            CodingHostedAuthoringReservation.evaluation_id
            == CodingHostedAssignment.evaluation_id,
        )
        .outerjoin(
            CodingHostedAuthoringFinalization,
            CodingHostedAuthoringFinalization.evaluation_id
            == CodingHostedAssignment.evaluation_id,
        )
        .outerjoin(
            CodingHostedGradingClaim,
            CodingHostedGradingClaim.evaluation_id
            == CodingHostedAssignment.evaluation_id,
        )
        .outerjoin(
            CodingHostedTerminalFinalization,
            CodingHostedTerminalFinalization.evaluation_id
            == CodingHostedAssignment.evaluation_id,
        )
        .outerjoin(grant, grant.evaluation_id == CodingHostedAssignment.evaluation_id)
        .join(inference, true())
        .join(deliveries, true())
        .where(CodingHostedAssignment.evaluation_id == evaluation_id)
    )


def hosted_assignment_detail(
    row: Mapping[Any, Any], deliveries: tuple[HostedDelivery, ...]
) -> HostedAssignmentDetail:
    """Build one detail from a single row, so every presence check and its
    values (terminal reservation and outcome included) come from one read."""
    terminal = None
    if row["terminal_reserved_at"] is not None:
        if row["terminal_outcome"] is None:
            raise ValueError("hosted terminal outcome is invalid")
        terminal = HostedTerminalStatus(
            outcome=row["terminal_outcome"],
            evidence_sha256=row["terminal_evidence_sha256"],
            reserved_at=row["terminal_reserved_at"],
            finalized_at=row["terminal_finalized_at"],
        )
    return HostedAssignmentDetail(
        **_summary_values(row, row["observed_at"]),
        observed_at=row["observed_at"],
        deadline_unix=row["deadline_unix"],
        selection_sha256=row["selection_sha256"],
        policy_sha256=row["policy_sha256"],
        execution_profile_sha256=row["execution_profile_sha256"],
        grading_profile_sha256=row["grading_profile_sha256"],
        admission_request_sha256=row["admission_request_sha256"],
        cancellation=(
            HostedCancellationRecord(
                assignment_sha256=row["cancellation_assignment_sha256"],
                prior_state=row["cancellation_prior_state"],
                reason=row["cancellation_reason"],
                actor=row["cancellation_actor"],
                cancelled_at=row["cancelled_at"],
            )
            if row["cancelled_at"] is not None
            else None
        ),
        private_task=(
            HostedPrivateTaskStatus(
                bound_at=row["task_bound_at"],
                selection_sha256=row["task_selection_sha256"],
                frozen_at=row["task_frozen_at"],
                closed_at=row["closed_at"],
                close_reason=row["close_reason"],
            )
            if row["task_bound_at"] is not None
            else None
        ),
        authoring_evidence_reserved_at=row["authoring_evidence_reserved_at"],
        authoring_evidence_finalized_at=row["authoring_evidence_finalized_at"],
        grading_claimed_at=row["grading_claimed_at"],
        terminal=terminal,
        inference=(
            HostedInferenceTotals(
                policy_sha256=row["inference_policy_sha256"],
                issued_at=row["inference_issued_at"],
                expires_at=row["inference_expires_at"],
                revoked_at=row["inference_revoked_at"],
                request_limit=row["inference_request_limit"],
                prompt_token_limit=row["inference_prompt_token_limit"],
                completion_token_limit=row["inference_completion_token_limit"],
                cost_usd_micros_limit=row["inference_cost_usd_micros_limit"],
                **{name: int(row[name]) for name in _INFERENCE_TOTALS},
            )
            if row["inference_issued_at"] is not None
            else None
        ),
        delivery_count=int(row["delivery_count"]),
        acknowledged_count=int(row["acknowledged_count"]),
        deliveries=deliveries,
    )


async def get_hosted_assignment_detail(
    session: AsyncSession, *, evaluation_id: UUID
) -> HostedAssignmentDetail:
    """Read one lifecycle in a single snapshot: one row, plus the newest deliveries."""
    async with session.begin():
        await _snapshot(session)
        row = (
            (await session.execute(_detail_query(evaluation_id)))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HostedAssignmentNotFoundError("hosted assignment does not exist")
        deliveries: tuple[HostedDelivery, ...] = ()
        if row["delivery_count"]:
            deliveries = tuple(
                HostedDelivery(**delivery)
                for delivery in (
                    await session.execute(
                        select(
                            CodingHostedResultDelivery.result_sha256,
                            CodingHostedResultDelivery.created_at.label("delivered_at"),
                            CodingHostedResultAcknowledgement.acknowledged_at,
                        )
                        .outerjoin(
                            CodingHostedResultAcknowledgement,
                            CodingHostedResultAcknowledgement.result_sha256
                            == CodingHostedResultDelivery.result_sha256,
                        )
                        .where(
                            CodingHostedResultDelivery.evaluation_id == evaluation_id
                        )
                        .order_by(
                            CodingHostedResultDelivery.created_at.desc(),
                            CodingHostedResultDelivery.result_sha256.desc(),
                        )
                        .limit(MAX_LISTED_DELIVERIES)
                    )
                ).mappings()
            )
        return hosted_assignment_detail(row, deliveries)


__all__ = [
    "MAX_LISTED_DELIVERIES",
    "HostedAssignmentDetail",
    "HostedAssignmentNotFoundError",
    "HostedAssignmentSummary",
    "HostedCancellation",
    "HostedCancellationError",
    "cancel_hosted_assignment",
    "get_hosted_assignment_detail",
    "hosted_assignment_detail",
    "hosted_operation_state",
    "list_hosted_assignment_summaries",
]
