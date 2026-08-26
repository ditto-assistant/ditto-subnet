"""Authenticated operator API for durable screening quarantines."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ditto.api_models.admin_quarantine import (
    AdminArtifactDuplicate,
    AdminBaselineDiffFileDetail,
    AdminBaselineDiffManifest,
    AdminBenchmarkContractMigrationDetail,
    AdminBenchmarkContractMigrationRequest,
    AdminBenchmarkContractMigrationResponse,
    AdminBenchmarkContractRefreshDetail,
    AdminBenchmarkContractRefreshRequest,
    AdminBenchmarkContractRefreshResponse,
    AdminBenchmarkQualificationDetail,
    AdminBenchmarkQualificationRequest,
    AdminBenchmarkQualificationResponse,
    AdminDuplicateSummary,
    AdminExpireRunningScreeningRequest,
    AdminExpireRunningScreeningResponse,
    AdminMinerContext,
    AdminMinerQuarantineSummary,
    AdminQuarantineAgentContext,
    AdminQuarantineBatchContextRequest,
    AdminQuarantineBatchContextResponse,
    AdminQuarantineBatchContextResult,
    AdminQuarantineBatchDecision,
    AdminQuarantineBatchExecuteItem,
    AdminQuarantineBatchExecuteRequest,
    AdminQuarantineBatchExecuteResponse,
    AdminQuarantineBatchPreviewItem,
    AdminQuarantineBatchPreviewRequest,
    AdminQuarantineBatchPreviewResponse,
    AdminQuarantineContext,
    AdminQuarantineItem,
    AdminQuarantineList,
    AdminQuarantineResolutionEvent,
    AdminQuarantineResolveRequest,
    AdminQuarantineResolveResponse,
    AdminScreenedImageRebuildDetail,
    AdminScreenedImageRebuildRequest,
    AdminScreenedImageRebuildResponse,
    AdminScreeningAttempt,
    AdminScreeningDisputeItem,
    AdminScreeningDisputeList,
    AdminScreeningDisputeResolveRequest,
    AdminScreeningDisputeResolveResponse,
    AdminScreeningFailureExample,
    AdminScreeningFailureGroup,
    AdminScreeningFailureSummary,
    AdminScreeningImageBuild,
    AdminScreeningRescreenRequest,
    AdminScreeningRescreenResponse,
    AdminScreeningRetryNowRequest,
    AdminScreeningRetryNowResponse,
    AdminScreeningSubmission,
    AdminScreeningSubmissionList,
    AdminSourceExcerpt,
    AdminSourceListing,
    AdminSourceSearchResult,
    AdminStarterKitProvenance,
    AdminValidatorAssignment,
    AdminValidatorAssignmentList,
    AdminValidatorAssignmentReleaseRequest,
    AdminValidatorAssignmentReleaseResponse,
)
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_models.screener import (
    SCREENING_POLICY_VERSION,
    ScreenEvidenceItem,
    ScreenReviewAudit,
    SourceReviewFinding,
)
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator import ArtifactResponse
from ditto.api_server.artifact_audit import client_ip, request_detail
from ditto.api_server.benchmark_rollout import (
    PendingQualification,
    inference_activation_requirements,
    qualification_candidate,
)
from ditto.api_server.datapipeline import DatasetGenerator
from ditto.api_server.dependencies import (
    get_dataset_generator,
    get_session,
    get_storage_client,
)
from ditto.api_server.endpoints.screener import _derive_dataset_seed
from ditto.api_server.endpoints.validator import ChainDep
from ditto.api_server.shadow_review import shadow_review_observation
from ditto.api_server.source_diff import (
    build_baseline_diff_manifest,
    unified_diff_for_file,
)
from ditto.api_server.source_inspect import (
    MAX_READ_LINES,
    MAX_SEARCH_CONTEXT,
    MAX_SEARCH_MATCHES,
    MAX_SEARCH_PATTERN_CHARS,
    MAX_TARBALL_BYTES,
    SourceInspectError,
    TarSourceInspector,
)
from ditto.api_server.starter_kit import (
    align_candidate_paths,
    is_stock_kit_text,
    starter_kit_head_text,
    starter_kit_provenance,
)
from ditto.api_server.storage import ObjectDownloadFailedError, S3StorageClient
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    EvaluationPayment,
    Score,
    ScreenerShadowReview,
    ScreeningAttempt,
    ScreeningDispute,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ScreeningRetryOverride,
    SubmissionImageBuild,
    SubmissionSourceReview,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_ADMIN_SCREENING_ARTIFACT,
    ENDPOINT_ADMIN_SOURCE_FILE,
    ENDPOINT_ADMIN_SOURCE_FILES,
    ENDPOINT_ADMIN_SOURCE_SEARCH,
    record_artifact_fetch,
)
from ditto.db.queries.audit import (
    append_audit_entry,
    benchmark_contract_refresh_event,
    screened_image_rebuild_event,
)
from ditto.db.queries.benchmark_rollout import (
    DatasetPin as RolloutDatasetPin,
)
from ditto.db.queries.benchmark_rollout import (
    active_bench_version,
    append_rollout_member,
    historical_rescore_cohort,
    maybe_activate_rollout,
    open_rollout,
)
from ditto.db.queries.payments import (
    get_miner_coldkey_for_agent,
    get_miner_coldkeys_for_agents,
)
from ditto.db.queries.tickets import RETRY_COOLDOWN, ticket_attempt_cap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GeneratorDep = Annotated[DatasetGenerator, Depends(get_dataset_generator)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
DatasetPin = tuple[int, int, str, str, int | None, str | None]
BATCH_PREVIEW_TTL = timedelta(minutes=10)


async def require_admin(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = request.app.state.config.admin_api_token
    if expected is None:
        raise HTTPException(status_code=503, detail="admin API is not configured")
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing admin bearer token")
    if not secrets.compare_digest(authorization[len(prefix) :], expected):
        raise HTTPException(status_code=401, detail="invalid admin bearer token")


AdminDep = Annotated[None, Depends(require_admin)]


def _review_payloads(
    row: ScreeningQuarantine, agent: Agent
) -> tuple[list[ScreenEvidenceItem] | None, SourceReviewFinding | None, bool]:
    """Parse the stored review payloads, tolerating legacy/foreign shapes.

    Rows written before the payloads landed have nulls; a row whose JSON no
    longer parses (schema drift) degrades to null rather than breaking the
    whole listing. ``finding_verified`` re-derives the digest binding at read
    time — and requires the finding to name THIS agent's artifact digest — so
    the console never has to trust a stored boolean and a finding copied from
    another submission can never present as verified.
    """
    evidence: list[ScreenEvidenceItem] | None = None
    if isinstance(row.evidence, list):
        try:
            evidence = [
                ScreenEvidenceItem.model_validate(item) for item in row.evidence[:16]
            ]
        except ValueError:
            evidence = None
    finding: SourceReviewFinding | None = None
    if isinstance(row.finding, dict):
        try:
            finding = SourceReviewFinding.model_validate(row.finding)
        except ValueError:
            finding = None
    verified = (
        finding is not None
        and row.finding_digest is not None
        and finding.canonical_digest() == row.finding_digest
        and finding.artifact_sha256 == agent.sha256
    )
    return evidence, finding, verified


def _item(
    row: ScreeningQuarantine,
    agent: Agent,
    history: list[ScreeningQuarantineResolution] | None = None,
    miner_coldkey: str | None = None,
) -> AdminQuarantineItem:
    evidence, finding, finding_verified = _review_payloads(row, agent)
    review_audit: ScreenReviewAudit | None = None
    if isinstance(row.review_audit, dict) and row.review_audit_digest is not None:
        try:
            parsed_audit = ScreenReviewAudit.model_validate(row.review_audit)
        except ValueError:
            pass
        else:
            if parsed_audit.canonical_digest() == row.review_audit_digest:
                review_audit = parsed_audit
    return AdminQuarantineItem(
        quarantine_id=row.quarantine_id,
        agent_id=row.agent_id,
        attempt_id=row.attempt_id,
        miner_hotkey=agent.miner_hotkey,
        miner_coldkey=miner_coldkey,
        agent_name=agent.name,
        agent_version=agent.version,
        artifact_sha256=agent.sha256,
        policy_version=row.policy_version,
        manifest_digest=row.manifest_digest,
        finding_digest=row.finding_digest,
        reason_code=row.reason_code,
        review_audit_digest=(
            row.review_audit_digest if review_audit is not None else None
        ),
        review_audit=review_audit,
        evidence=evidence,
        finding=finding,
        finding_verified=finding_verified,
        status=row.status,  # type: ignore[arg-type]
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        resolution=row.resolution,  # type: ignore[arg-type]
        resolution_reason=row.resolution_reason,
        resolution_history=[
            AdminQuarantineResolutionEvent(
                resolution=event.resolution,  # type: ignore[arg-type]
                reason=event.reason,
                actor=event.actor,
                created_at=event.created_at,
            )
            for event in history or []
        ],
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _resolution_history(
    session: AsyncSession, quarantine_ids: list[UUID]
) -> dict[UUID, list[ScreeningQuarantineResolution]]:
    history: dict[UUID, list[ScreeningQuarantineResolution]] = defaultdict(list)
    if not quarantine_ids:
        return history
    events = await session.scalars(
        select(ScreeningQuarantineResolution)
        .where(ScreeningQuarantineResolution.quarantine_id.in_(quarantine_ids))
        .order_by(
            ScreeningQuarantineResolution.created_at,
            ScreeningQuarantineResolution.resolution_id,
        )
    )
    for event in events:
        history[event.quarantine_id].append(event)
    return history


async def _prepare_release_dataset(
    session: AsyncSession,
    chain: ChainDep,
    generator: DatasetGenerator,
    quarantine_id: UUID,
) -> DatasetPin | None:
    run_size = generator.run_size
    if run_size is None:
        return None
    async with session.begin():
        agent = await session.scalar(
            select(Agent)
            .join(
                ScreeningQuarantine,
                ScreeningQuarantine.agent_id == Agent.agent_id,
            )
            .where(ScreeningQuarantine.quarantine_id == quarantine_id)
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="quarantine not found")
        bench_version = await active_bench_version(session)
        versioned_dataset = await session.get(
            BenchmarkDataset, (agent.agent_id, bench_version)
        )
        existing_seed = agent.dataset_seed
        existing_seed_block = agent.dataset_seed_block
        existing_seed_block_hash = agent.dataset_seed_block_hash
    if versioned_dataset is not None:
        return None
    if existing_seed is None:
        seed, block_number, block_hash = await _derive_dataset_seed(
            chain, agent.agent_id
        )
    else:
        seed = existing_seed
        block_number = existing_seed_block
        block_hash = existing_seed_block_hash
    dataset_sha256 = await generator.generate(seed, bench_version=bench_version)
    return (
        bench_version,
        seed,
        dataset_sha256,
        run_size,
        block_number,
        block_hash,
    )


async def _apply_dataset(
    session: AsyncSession, agent: Agent, dataset: DatasetPin | None
) -> None:
    if dataset is None:
        return
    (
        bench_version,
        seed,
        dataset_sha256,
        run_size,
        block_number,
        block_hash,
    ) = dataset
    existing = await session.get(BenchmarkDataset, (agent.agent_id, bench_version))
    if existing is None:
        session.add(
            BenchmarkDataset(
                agent_id=agent.agent_id,
                bench_version=bench_version,
                seed=seed,
                sha256=dataset_sha256,
                run_size=run_size,
                seed_block=block_number,
                seed_block_hash=block_hash,
            )
        )
    elif (
        existing.seed,
        existing.sha256,
        existing.run_size,
        existing.seed_block,
        existing.seed_block_hash,
    ) != (seed, dataset_sha256, run_size, block_number, block_hash):
        raise HTTPException(
            status_code=409,
            detail="active benchmark dataset changed during quarantine release",
        )
    # Preserve the original/v2 compatibility pin. A current-version release may
    # backfill a newer BenchmarkDataset row but must not rewrite older scores'
    # dataset authority.
    if agent.dataset_seed is not None:
        return
    (
        agent.dataset_seed,
        agent.dataset_sha256,
        agent.dataset_run_size,
        agent.dataset_seed_block,
        agent.dataset_seed_block_hash,
    ) = (seed, dataset_sha256, run_size, block_number, block_hash)


def _preview_signature_payload(
    actor: str,
    decisions: list[AdminQuarantineBatchDecision],
    issued_at: int,
) -> bytes:
    return json.dumps(
        {
            "actor": actor,
            "decisions": [decision.model_dump(mode="json") for decision in decisions],
            "issued_at": issued_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sign_batch_preview(
    secret: str,
    actor: str,
    decisions: list[AdminQuarantineBatchDecision],
    issued_at: int,
) -> str:
    digest = hmac.new(
        secret.encode(),
        _preview_signature_payload(actor, decisions, issued_at),
        hashlib.sha256,
    ).hexdigest()
    return f"{issued_at}.{digest}"


def _verify_batch_preview(
    token: str,
    secret: str,
    actor: str,
    decisions: list[AdminQuarantineBatchDecision],
) -> None:
    try:
        issued_text, _digest = token.split(".", 1)
        issued_at = int(issued_text)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail="invalid batch preview token"
        ) from None
    now = int(datetime.now(UTC).timestamp())
    if issued_at > now + 30 or now - issued_at > int(BATCH_PREVIEW_TTL.total_seconds()):
        raise HTTPException(
            status_code=409, detail="batch preview expired; preview again"
        )
    expected = _sign_batch_preview(secret, actor, decisions, issued_at)
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=409,
            detail="batch decisions changed after preview; preview again",
        )


def _dispute_item(
    dispute: ScreeningDispute,
    agent: Agent,
    quarantine: ScreeningQuarantine,
    history: list[ScreeningQuarantineResolution],
) -> AdminScreeningDisputeItem:
    original_reason = next(
        (event.reason for event in history if event.resolution == "reject"),
        quarantine.resolution_reason,
    )
    return AdminScreeningDisputeItem(
        dispute_id=dispute.dispute_id,
        agent_id=dispute.agent_id,
        quarantine_id=dispute.quarantine_id,
        miner_hotkey=dispute.miner_hotkey,
        agent_name=agent.name,
        agent_version=agent.version,
        artifact_sha256=agent.sha256,
        message=dispute.message,
        status=dispute.status,  # type: ignore[arg-type]
        created_at=dispute.created_at,
        original_reason=original_reason,
        resolved_at=dispute.resolved_at,
        resolved_by=dispute.resolved_by,
        resolution=dispute.resolution,  # type: ignore[arg-type]
        resolution_reason=dispute.resolution_reason,
    )


@router.get("/validator-assignments", response_model=AdminValidatorAssignmentList)
async def list_validator_assignments(
    _admin: AdminDep,
    session: SessionDep,
) -> AdminValidatorAssignmentList:
    """List live scoring leases for operator recovery tooling."""
    now = datetime.now(UTC)
    score_count = (
        select(func.count(Score.validator_hotkey))
        .where(
            Score.agent_id == ValidatorTicket.agent_id,
            Score.details["bench_version"].as_integer()
            == ValidatorTicket.bench_version,
        )
        .correlate(ValidatorTicket)
        .scalar_subquery()
    )
    provisional_composite = (
        select(func.avg(Score.composite))
        .where(
            Score.agent_id == ValidatorTicket.agent_id,
            Score.details["bench_version"].as_integer()
            == ValidatorTicket.bench_version,
        )
        .correlate(ValidatorTicket)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(
                ValidatorTicket,
                Agent,
                score_count,
                provisional_composite,
            )
            .join(Agent, Agent.agent_id == ValidatorTicket.agent_id)
            .where(
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
            )
            .order_by(ValidatorTicket.deadline.asc(), ValidatorTicket.agent_id.asc())
        )
    ).all()
    items = [
        AdminValidatorAssignment(
            agent_id=ticket.agent_id,
            agent_name=agent.name,
            miner_hotkey=agent.miner_hotkey,
            validator_hotkey=ticket.validator_hotkey,
            issued_at=ticket.issued_at,
            deadline=ticket.deadline,
            bench_version=ticket.bench_version,
            attempt_count=ticket.attempt_count,
            score_count=int(score_count),
            provisional_composite=(
                float(provisional_composite)
                if provisional_composite is not None
                else None
            ),
            slot_id=ticket.slot_id,
            purpose=str(ticket.purpose),  # type: ignore[arg-type]
            agent_status=agent.status.value,
            first_reported_at=ticket.first_reported_at,
        )
        for ticket, agent, score_count, provisional_composite in rows
    ]
    return AdminValidatorAssignmentList(items=items, count=len(items))


@router.post(
    "/validator-assignments/{agent_id}/{validator_hotkey}/release",
    response_model=AdminValidatorAssignmentReleaseResponse,
)
async def release_validator_assignment(
    agent_id: UUID,
    validator_hotkey: str,
    payload: AdminValidatorAssignmentReleaseRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidatorAssignmentReleaseResponse:
    """Expire one exact live lease without deleting its submission or scores."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")

    now = datetime.now(UTC)
    async with session.begin():
        ticket = await session.scalar(
            select(ValidatorTicket)
            .where(
                ValidatorTicket.agent_id == agent_id,
                ValidatorTicket.validator_hotkey == validator_hotkey,
            )
            .with_for_update()
        )
        if ticket is None:
            raise HTTPException(
                status_code=404, detail="validator assignment not found"
            )
        if (
            ticket.status != TicketStatus.ISSUED
            or _as_utc(ticket.deadline) <= now
            or _as_utc(ticket.deadline) != _as_utc(payload.expected_deadline)
        ):
            raise HTTPException(
                status_code=409,
                detail="validator assignment changed or is no longer active",
            )
        ticket.status = TicketStatus.EXPIRED
        # Manual release starts the same full cooldown from the operator's
        # intervention. Using the original future deadline would make a stuck
        # assignment wait longer than the standard retry interval after it has
        # already been explicitly cleared.
        ticket.retry_after = now + RETRY_COOLDOWN

    logger.warning(
        "admin released validator assignment actor=%s agent_id=%s validator=%s "
        "deadline=%s retry_after=%s reason=%r",
        x_admin_actor,
        agent_id,
        validator_hotkey,
        payload.expected_deadline.isoformat(),
        ticket.retry_after.isoformat(),
        payload.reason,
    )
    return AdminValidatorAssignmentReleaseResponse(
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        status="expired",
        retry_after=ticket.retry_after,
    )


@router.get("/screening-quarantines", response_model=AdminQuarantineList)
async def list_quarantines(
    _admin: AdminDep,
    session: SessionDep,
    status: Annotated[Literal["active", "resolved", "all"], Query()] = "active",
    sort: Annotated[Literal["oldest", "newest"], Query()] = "oldest",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminQuarantineList:
    order = (
        (
            ScreeningQuarantine.created_at.asc(),
            ScreeningQuarantine.quarantine_id.asc(),
        )
        if sort == "oldest"
        else (
            ScreeningQuarantine.created_at.desc(),
            ScreeningQuarantine.quarantine_id.desc(),
        )
    )
    stmt = (
        select(ScreeningQuarantine, Agent)
        .join(Agent, Agent.agent_id == ScreeningQuarantine.agent_id)
        .order_by(*order)
        .offset(offset)
        .limit(limit)
    )
    if status != "all":
        stmt = stmt.where(ScreeningQuarantine.status == status)
    count_stmt = select(func.count()).select_from(ScreeningQuarantine)
    if status != "all":
        count_stmt = count_stmt.where(ScreeningQuarantine.status == status)
    total = int((await session.scalar(count_stmt)) or 0)
    rows = (await session.execute(stmt)).all()
    history = await _resolution_history(
        session, [quarantine.quarantine_id for quarantine, _agent in rows]
    )
    coldkeys = await get_miner_coldkeys_for_agents(
        session, agent_ids={agent.agent_id for _quarantine, agent in rows}
    )
    items = [
        _item(
            quarantine,
            agent,
            history[quarantine.quarantine_id],
            coldkeys.get(agent.agent_id),
        )
        for quarantine, agent in rows
    ]
    return AdminQuarantineList(items=items, count=total)


@router.post(
    "/screening-quarantines/batch-context",
    response_model=AdminQuarantineBatchContextResponse,
)
async def get_quarantine_batch_context(
    payload: AdminQuarantineBatchContextRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminQuarantineBatchContextResponse:
    """Fetch bounded full-review contexts without one HTTP round-trip per row."""
    if len(set(payload.quarantine_ids)) != len(payload.quarantine_ids):
        raise HTTPException(status_code=422, detail="quarantine_ids must be unique")
    items: list[AdminQuarantineBatchContextResult] = []
    for quarantine_id in payload.quarantine_ids:
        try:
            context = await _build_quarantine_context(quarantine_id, session)
            items.append(
                AdminQuarantineBatchContextResult(
                    quarantine_id=quarantine_id,
                    context=context,
                )
            )
        except HTTPException as exc:
            items.append(
                AdminQuarantineBatchContextResult(
                    quarantine_id=quarantine_id,
                    error=str(exc.detail),
                )
            )
    return AdminQuarantineBatchContextResponse(items=items, count=len(items))


async def _preview_batch_decision(
    session: AsyncSession,
    decision: AdminQuarantineBatchDecision,
    actor: str,
) -> AdminQuarantineBatchPreviewItem:
    result = (
        await session.execute(
            select(ScreeningQuarantine, Agent)
            .join(Agent, Agent.agent_id == ScreeningQuarantine.agent_id)
            .where(ScreeningQuarantine.quarantine_id == decision.quarantine_id)
        )
    ).one_or_none()
    if result is None:
        return AdminQuarantineBatchPreviewItem(
            quarantine_id=decision.quarantine_id,
            resolution=decision.resolution,
            reason=decision.reason,
            disposition="not_found",
            message="quarantine not found",
        )
    quarantine, agent = result
    base = {
        "quarantine_id": decision.quarantine_id,
        "agent_id": agent.agent_id,
        "agent_name": agent.name,
        "artifact_sha256": agent.sha256,
        "resolution": decision.resolution,
        "reason": decision.reason,
    }
    if (
        agent.agent_id != decision.expected_agent_id
        or agent.sha256 != decision.expected_artifact_sha256
    ):
        return AdminQuarantineBatchPreviewItem(
            **base,
            disposition="conflict",
            message="submission identity changed",
        )
    target = {
        "release": AgentStatus.EVALUATING,
        "rescreen": AgentStatus.SCREENING_FAILED,
        "reject": AgentStatus.REJECTED,
    }[decision.resolution]
    if (
        quarantine.status == "resolved"
        and quarantine.resolution == decision.resolution
        and quarantine.resolution_reason == decision.reason
        and quarantine.resolved_by == actor
        and agent.status == target
    ):
        return AdminQuarantineBatchPreviewItem(
            **base,
            disposition="already_applied",
            resulting_agent_status=target,
            message="this exact operator decision is already recorded",
        )
    is_initial = (
        quarantine.status == "active" and agent.status == AgentStatus.QUARANTINED
    )
    is_correction = (
        quarantine.status == "resolved"
        and quarantine.resolution == "reject"
        and agent.status == AgentStatus.REJECTED
        and decision.resolution == "release"
    )
    if not is_initial and not is_correction:
        return AdminQuarantineBatchPreviewItem(
            **base,
            disposition="conflict",
            message="quarantine is no longer actionable with this decision",
        )
    return AdminQuarantineBatchPreviewItem(
        **base,
        disposition="ready",
        resulting_agent_status=target,
        message=f"will set submission status to {target}",
    )


@router.post(
    "/screening-quarantines/batch-preview",
    response_model=AdminQuarantineBatchPreviewResponse,
)
async def preview_quarantine_batch(
    payload: AdminQuarantineBatchPreviewRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminQuarantineBatchPreviewResponse:
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    ids = [decision.quarantine_id for decision in payload.decisions]
    if len(set(ids)) != len(ids):
        raise HTTPException(
            status_code=422, detail="quarantine decisions must be unique"
        )
    items = [
        await _preview_batch_decision(session, decision, x_admin_actor)
        for decision in payload.decisions
    ]
    issued_at = int(datetime.now(UTC).timestamp())
    secret = request.app.state.config.admin_api_token
    assert secret is not None
    return AdminQuarantineBatchPreviewResponse(
        preview_token=_sign_batch_preview(
            secret, x_admin_actor, payload.decisions, issued_at
        ),
        expires_at=datetime.fromtimestamp(issued_at, UTC) + BATCH_PREVIEW_TTL,
        items=items,
        ready_count=sum(item.disposition == "ready" for item in items),
        already_applied_count=sum(
            item.disposition == "already_applied" for item in items
        ),
        blocked_count=sum(
            item.disposition in {"conflict", "not_found"} for item in items
        ),
    )


@router.post(
    "/screening-quarantines/batch-resolve",
    response_model=AdminQuarantineBatchExecuteResponse,
)
async def execute_quarantine_batch(
    payload: AdminQuarantineBatchExecuteRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    chain: ChainDep,
    generator: GeneratorDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminQuarantineBatchExecuteResponse:
    """Apply separately audited decisions; failures never hide successful rows."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    ids = [decision.quarantine_id for decision in payload.decisions]
    if len(set(ids)) != len(ids):
        raise HTTPException(
            status_code=422, detail="quarantine decisions must be unique"
        )
    secret = request.app.state.config.admin_api_token
    assert secret is not None
    _verify_batch_preview(
        payload.preview_token,
        secret,
        x_admin_actor,
        payload.decisions,
    )

    results: list[AdminQuarantineBatchExecuteItem] = []
    for decision in payload.decisions:
        try:
            preview = await _preview_batch_decision(session, decision, x_admin_actor)
            # End the read-only implicit transaction before the per-item write
            # transaction (and before release dataset preparation).
            await session.rollback()
            if preview.disposition == "already_applied":
                results.append(
                    AdminQuarantineBatchExecuteItem(
                        quarantine_id=decision.quarantine_id,
                        status="already_applied",
                        agent_status=preview.resulting_agent_status,
                        message=preview.message,
                    )
                )
                continue
            if preview.disposition != "ready":
                results.append(
                    AdminQuarantineBatchExecuteItem(
                        quarantine_id=decision.quarantine_id,
                        status="failed",
                        message=preview.message,
                    )
                )
                continue
            new_dataset = (
                await _prepare_release_dataset(
                    session, chain, generator, decision.quarantine_id
                )
                if decision.resolution == "release"
                else None
            )
            async with session.begin():
                quarantine = await session.scalar(
                    select(ScreeningQuarantine)
                    .where(ScreeningQuarantine.quarantine_id == decision.quarantine_id)
                    .with_for_update()
                )
                if quarantine is None:
                    raise HTTPException(status_code=404, detail="quarantine not found")
                agent = await session.scalar(
                    select(Agent)
                    .where(Agent.agent_id == quarantine.agent_id)
                    .with_for_update()
                )
                if agent is None:
                    raise HTTPException(status_code=404, detail="agent not found")
                if (
                    agent.agent_id != decision.expected_agent_id
                    or agent.sha256 != decision.expected_artifact_sha256
                ):
                    raise HTTPException(
                        status_code=409, detail="submission identity changed"
                    )
                is_initial = (
                    quarantine.status == "active"
                    and agent.status == AgentStatus.QUARANTINED
                )
                is_correction = (
                    quarantine.status == "resolved"
                    and quarantine.resolution == "reject"
                    and agent.status == AgentStatus.REJECTED
                    and decision.resolution == "release"
                )
                if not is_initial and not is_correction:
                    raise HTTPException(
                        status_code=409,
                        detail="quarantine changed after preview",
                    )
                target = {
                    "release": AgentStatus.EVALUATING,
                    "rescreen": AgentStatus.SCREENING_FAILED,
                    "reject": AgentStatus.REJECTED,
                }[decision.resolution]
                now = datetime.now(UTC)
                agent.status = target
                agent.screening_reason = decision.reason
                await _apply_dataset(session, agent, new_dataset)
                quarantine.status = "resolved"
                quarantine.resolved_at = now
                quarantine.resolved_by = x_admin_actor
                quarantine.resolution = decision.resolution
                quarantine.resolution_reason = decision.reason
                session.add(
                    ScreeningQuarantineResolution(
                        resolution_id=uuid4(),
                        quarantine_id=quarantine.quarantine_id,
                        resolution=decision.resolution,
                        reason=decision.reason,
                        actor=x_admin_actor,
                        created_at=now,
                    )
                )
            results.append(
                AdminQuarantineBatchExecuteItem(
                    quarantine_id=decision.quarantine_id,
                    status="applied",
                    agent_status=target,
                    message="decision applied and audit event recorded",
                )
            )
        except HTTPException as exc:
            results.append(
                AdminQuarantineBatchExecuteItem(
                    quarantine_id=decision.quarantine_id,
                    status="failed",
                    message=str(exc.detail),
                )
            )
        except Exception:
            logger.exception(
                "batch quarantine resolution failed actor=%s quarantine_id=%s",
                x_admin_actor,
                decision.quarantine_id,
            )
            results.append(
                AdminQuarantineBatchExecuteItem(
                    quarantine_id=decision.quarantine_id,
                    status="failed",
                    message="internal error while applying decision",
                )
            )
    return AdminQuarantineBatchExecuteResponse(
        items=results,
        applied_count=sum(item.status == "applied" for item in results),
        already_applied_count=sum(item.status == "already_applied" for item in results),
        failed_count=sum(item.status == "failed" for item in results),
    )


@router.get(
    "/screening-quarantines/{quarantine_id}", response_model=AdminQuarantineItem
)
async def get_quarantine(
    quarantine_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminQuarantineItem:
    result = (
        await session.execute(
            select(ScreeningQuarantine, Agent)
            .join(Agent, Agent.agent_id == ScreeningQuarantine.agent_id)
            .where(ScreeningQuarantine.quarantine_id == quarantine_id)
        )
    ).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="quarantine not found")
    quarantine, agent = result
    history = await _resolution_history(session, [quarantine.quarantine_id])
    coldkey = await get_miner_coldkey_for_agent(session, agent_id=agent.agent_id)
    return _item(quarantine, agent, history[quarantine.quarantine_id], coldkey)


async def _build_quarantine_context(
    quarantine_id: UUID, session: AsyncSession
) -> AdminQuarantineContext:
    """One-stop review context: finding, attempts, miner history, duplicates."""
    result = (
        await session.execute(
            select(ScreeningQuarantine, Agent)
            .join(Agent, Agent.agent_id == ScreeningQuarantine.agent_id)
            .where(ScreeningQuarantine.quarantine_id == quarantine_id)
        )
    ).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="quarantine not found")
    quarantine, agent = result

    attempts = (
        (
            await session.execute(
                select(ScreeningAttempt)
                .where(ScreeningAttempt.agent_id == agent.agent_id)
                .order_by(
                    ScreeningAttempt.started_at.desc(),
                    ScreeningAttempt.attempt_id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )

    # Advisory L2/L3 telemetry for this quarantine's own attempt. Optional by
    # construction: shadow mode can be off, and quarantines predating the
    # reviewer have no row at all.
    shadow_row = await session.scalar(
        select(ScreenerShadowReview).where(
            ScreenerShadowReview.attempt_id == quarantine.attempt_id
        )
    )

    total_submissions = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Agent)
                .where(Agent.miner_hotkey == agent.miner_hotkey)
            )
        )
        or 0
    )
    # Aggregate in SQL: a prolific miner must not make the console
    # materialize their entire quarantine history per request.
    resolution_rows = (
        await session.execute(
            select(ScreeningQuarantine.resolution, func.count())
            .join(Agent, Agent.agent_id == ScreeningQuarantine.agent_id)
            .where(Agent.miner_hotkey == agent.miner_hotkey)
            .group_by(ScreeningQuarantine.resolution)
        )
    ).all()
    resolution_counts = {
        resolution: int(count) for resolution, count in resolution_rows
    }
    quarantine_count = sum(resolution_counts.values())
    miner_rows = (
        await session.execute(
            select(ScreeningQuarantine, Agent)
            .join(Agent, Agent.agent_id == ScreeningQuarantine.agent_id)
            .where(
                Agent.miner_hotkey == agent.miner_hotkey,
                ScreeningQuarantine.quarantine_id != quarantine_id,
            )
            .order_by(
                ScreeningQuarantine.created_at.desc(),
                ScreeningQuarantine.quarantine_id.desc(),
            )
            .limit(10)
        )
    ).all()
    recent = [
        AdminMinerQuarantineSummary(
            quarantine_id=row.quarantine_id,
            agent_id=row.agent_id,
            agent_name=other.name,
            reason_code=row.reason_code,
            status=row.status,  # type: ignore[arg-type]
            resolution=row.resolution,  # type: ignore[arg-type]
            resolution_reason=row.resolution_reason,
            created_at=row.created_at,
            resolved_at=row.resolved_at,
        )
        for row, other in miner_rows
    ]

    # Exact-duplicate signals only: identical tarball bytes, or identical
    # canonicalized source (reformat/re-comment/reorder repack). Fuzzy MinHash
    # similarity stays in the scoring gate; here a hit must be self-evident.
    duplicate_conditions = [Agent.sha256 == agent.sha256]
    if agent.normalized_source_hash is not None:
        duplicate_conditions.append(
            Agent.normalized_source_hash == agent.normalized_source_hash
        )
    duplicate_filter = (
        Agent.agent_id != agent.agent_id,
        or_(*duplicate_conditions),
    )
    # Authoritative aggregate counts, independent of the bounded sample below:
    # attribution claims ("another miner submitted this exact code") must
    # never be derived from whether a 20-row sample happened to include one.
    cross_miner_count = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Agent)
                .where(*duplicate_filter, Agent.miner_hotkey != agent.miner_hotkey)
            )
        )
        or 0
    )
    same_miner_count = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Agent)
                .where(*duplicate_filter, Agent.miner_hotkey == agent.miner_hotkey)
            )
        )
        or 0
    )
    candidate_coldkey = await session.scalar(
        select(EvaluationPayment.miner_coldkey).where(
            EvaluationPayment.agent_id == agent.agent_id
        )
    )
    # Every coldkey that ever funded THIS hotkey. Usually one; several is
    # ordinary miner behaviour, and the reviewer needs to see it rather than
    # infer it from a single submission's payment row.
    miner_coldkeys = sorted(
        {
            coldkey
            for coldkey in (
                await session.scalars(
                    select(EvaluationPayment.miner_coldkey)
                    .where(EvaluationPayment.miner_hotkey == agent.miner_hotkey)
                    .distinct()
                )
            ).all()
            if coldkey is not None
        }
    )
    duplicate_payment = aliased(EvaluationPayment)
    same_owner_filter = Agent.miner_hotkey == agent.miner_hotkey
    if candidate_coldkey is not None:
        same_owner_filter = or_(
            same_owner_filter,
            duplicate_payment.miner_coldkey == candidate_coldkey,
        )
    cross_owner_filter = Agent.miner_hotkey != agent.miner_hotkey
    if candidate_coldkey is not None:
        cross_owner_filter = and_(
            cross_owner_filter,
            or_(
                duplicate_payment.miner_coldkey.is_(None),
                duplicate_payment.miner_coldkey != candidate_coldkey,
            ),
        )
    same_owner_count = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Agent)
                .outerjoin(
                    duplicate_payment,
                    duplicate_payment.agent_id == Agent.agent_id,
                )
                .where(*duplicate_filter, same_owner_filter)
            )
        )
        or 0
    )
    cross_owner_count = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Agent)
                .outerjoin(
                    duplicate_payment,
                    duplicate_payment.agent_id == Agent.agent_id,
                )
                .where(*duplicate_filter, cross_owner_filter)
            )
        )
        or 0
    )
    duplicate_rows = (
        await session.execute(
            select(Agent, duplicate_payment.miner_coldkey)
            .outerjoin(
                duplicate_payment,
                duplicate_payment.agent_id == Agent.agent_id,
            )
            .where(*duplicate_filter)
            .order_by(Agent.created_at.desc(), Agent.agent_id.desc())
            .limit(20)
        )
    ).all()
    duplicates = [
        AdminArtifactDuplicate(
            agent_id=other.agent_id,
            miner_hotkey=other.miner_hotkey,
            agent_name=other.name,
            agent_status=other.status,
            submitted_at=other.created_at,
            miner_coldkey=other_coldkey,
            match=(
                "identical_artifact"
                if other.sha256 == agent.sha256
                else "identical_normalized_source"
            ),
            same_owner=(
                other.miner_hotkey == agent.miner_hotkey
                or bool(
                    candidate_coldkey is not None and other_coldkey == candidate_coldkey
                )
            ),
        )
        for other, other_coldkey in duplicate_rows
    ]

    return AdminQuarantineContext(
        quarantine=_item(quarantine, agent, None, candidate_coldkey),
        agent=AdminQuarantineAgentContext(
            agent_id=agent.agent_id,
            miner_hotkey=agent.miner_hotkey,
            miner_coldkey=candidate_coldkey,
            agent_name=agent.name,
            artifact_sha256=agent.sha256,
            agent_status=agent.status,
            size_bytes=agent.size_bytes,
            submitted_at=agent.created_at,
            screening_policy_version=agent.screening_policy_version,
            screening_reason=agent.screening_reason,
        ),
        attempts=[
            AdminScreeningAttempt(
                attempt_id=attempt.attempt_id,
                policy_version=attempt.policy_version,
                status=attempt.status,  # type: ignore[arg-type]
                screener_hotkey=attempt.screener_hotkey,
                started_at=attempt.started_at,
                deadline=attempt.deadline,
                finished_at=attempt.finished_at,
                reason=attempt.public_reason,
                reason_code=attempt.reason_code,
                duplicate_of=attempt.duplicate_of,
            )
            for attempt in attempts
        ],
        miner=AdminMinerContext(
            miner_hotkey=agent.miner_hotkey,
            miner_coldkeys=miner_coldkeys,
            total_submissions=total_submissions,
            quarantine_count=quarantine_count,
            released_count=resolution_counts.get("release", 0),
            rescreened_count=resolution_counts.get("rescreen", 0),
            rejected_count=resolution_counts.get("reject", 0),
            recent_quarantines=recent,
        ),
        duplicates=duplicates,
        duplicate_summary=AdminDuplicateSummary(
            total=cross_miner_count + same_miner_count,
            cross_miner=cross_miner_count,
            same_miner=same_miner_count,
            cross_owner=cross_owner_count,
            same_owner=same_owner_count,
            sample_truncated=cross_owner_count + same_owner_count > len(duplicate_rows),
        ),
        shadow_review=(
            shadow_review_observation(shadow_row) if shadow_row is not None else None
        ),
    )


@router.get(
    "/screening-quarantines/{quarantine_id}/context",
    response_model=AdminQuarantineContext,
)
async def get_quarantine_context(
    quarantine_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminQuarantineContext:
    return await _build_quarantine_context(quarantine_id, session)


@router.post(
    "/screening-quarantines/{quarantine_id}/resolve",
    response_model=AdminQuarantineResolveResponse,
)
async def resolve_quarantine(
    quarantine_id: UUID,
    payload: AdminQuarantineResolveRequest,
    _admin: AdminDep,
    session: SessionDep,
    chain: ChainDep,
    generator: GeneratorDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminQuarantineResolveResponse:
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")

    new_dataset = (
        await _prepare_release_dataset(session, chain, generator, quarantine_id)
        if payload.resolution == "release"
        else None
    )

    async with session.begin():
        quarantine = await session.scalar(
            select(ScreeningQuarantine)
            .where(ScreeningQuarantine.quarantine_id == quarantine_id)
            .with_for_update()
        )
        if quarantine is None:
            raise HTTPException(status_code=404, detail="quarantine not found")
        agent = await session.scalar(
            select(Agent).where(Agent.agent_id == quarantine.agent_id).with_for_update()
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        is_initial_resolution = (
            quarantine.status == "active" and agent.status == AgentStatus.QUARANTINED
        )
        is_rejection_correction = (
            quarantine.status == "resolved"
            and quarantine.resolution == "reject"
            and agent.status == AgentStatus.REJECTED
            and payload.resolution == "release"
        )
        if not is_initial_resolution and not is_rejection_correction:
            raise HTTPException(
                status_code=409,
                detail="quarantine is not active or a correctable rejection",
            )

        target = {
            "release": AgentStatus.EVALUATING,
            "rescreen": AgentStatus.SCREENING_FAILED,
            "reject": AgentStatus.REJECTED,
        }[payload.resolution]
        agent.status = target
        agent.screening_reason = payload.reason
        await _apply_dataset(session, agent, new_dataset)
        quarantine.status = "resolved"
        quarantine.resolved_at = datetime.now(UTC)
        quarantine.resolved_by = x_admin_actor
        quarantine.resolution = payload.resolution
        quarantine.resolution_reason = payload.reason
        session.add(
            ScreeningQuarantineResolution(
                resolution_id=uuid4(),
                quarantine_id=quarantine.quarantine_id,
                resolution=payload.resolution,
                reason=payload.reason,
                actor=x_admin_actor,
                created_at=quarantine.resolved_at,
            )
        )

    history = await _resolution_history(session, [quarantine.quarantine_id])
    coldkey = await get_miner_coldkey_for_agent(session, agent_id=agent.agent_id)
    return AdminQuarantineResolveResponse(
        quarantine=_item(quarantine, agent, history[quarantine.quarantine_id], coldkey),
        agent_status=agent.status,
    )


@router.get("/screening-disputes", response_model=AdminScreeningDisputeList)
async def list_screening_disputes(
    _admin: AdminDep,
    session: SessionDep,
    status: Annotated[Literal["pending", "resolved", "all"], Query()] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminScreeningDisputeList:
    stmt = (
        select(ScreeningDispute, Agent, ScreeningQuarantine)
        .join(Agent, Agent.agent_id == ScreeningDispute.agent_id)
        .join(
            ScreeningQuarantine,
            ScreeningQuarantine.quarantine_id == ScreeningDispute.quarantine_id,
        )
        .order_by(ScreeningDispute.created_at, ScreeningDispute.dispute_id)
        .offset(offset)
        .limit(limit)
    )
    count_stmt = select(func.count()).select_from(ScreeningDispute)
    if status != "all":
        stmt = stmt.where(ScreeningDispute.status == status)
        count_stmt = count_stmt.where(ScreeningDispute.status == status)
    rows = (await session.execute(stmt)).all()
    history = await _resolution_history(
        session, [dispute.quarantine_id for dispute, _agent, _quarantine in rows]
    )
    return AdminScreeningDisputeList(
        items=[
            _dispute_item(
                dispute,
                agent,
                quarantine,
                history[dispute.quarantine_id],
            )
            for dispute, agent, quarantine in rows
        ],
        count=int((await session.scalar(count_stmt)) or 0),
    )


@router.post(
    "/screening-disputes/{dispute_id}/resolve",
    response_model=AdminScreeningDisputeResolveResponse,
)
async def resolve_screening_dispute(
    dispute_id: UUID,
    payload: AdminScreeningDisputeResolveRequest,
    _admin: AdminDep,
    session: SessionDep,
    chain: ChainDep,
    generator: GeneratorDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminScreeningDisputeResolveResponse:
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")

    new_dataset: DatasetPin | None = None
    if payload.resolution == "release":
        existing = await session.get(ScreeningDispute, dispute_id)
        quarantine_id = existing.quarantine_id if existing is not None else None
        await session.rollback()
        if quarantine_id is None:
            raise HTTPException(status_code=404, detail="dispute not found")
        new_dataset = await _prepare_release_dataset(
            session, chain, generator, quarantine_id
        )

    async with session.begin():
        dispute = await session.scalar(
            select(ScreeningDispute)
            .where(ScreeningDispute.dispute_id == dispute_id)
            .with_for_update()
        )
        if dispute is None:
            raise HTTPException(status_code=404, detail="dispute not found")
        quarantine = await session.scalar(
            select(ScreeningQuarantine)
            .where(ScreeningQuarantine.quarantine_id == dispute.quarantine_id)
            .with_for_update()
        )
        agent = await session.scalar(
            select(Agent).where(Agent.agent_id == dispute.agent_id).with_for_update()
        )
        if quarantine is None or agent is None:
            raise HTTPException(status_code=404, detail="disputed submission not found")
        if dispute.status != "pending":
            raise HTTPException(status_code=409, detail="dispute is already resolved")
        if (
            agent.status != AgentStatus.REJECTED
            or quarantine.status != "resolved"
            or quarantine.resolution != "reject"
        ):
            raise HTTPException(
                status_code=409,
                detail="the disputed rejection is no longer current",
            )

        now = datetime.now(UTC)
        if payload.resolution == "release":
            agent.status = AgentStatus.EVALUATING
            agent.screening_reason = payload.reason
            await _apply_dataset(session, agent, new_dataset)
            quarantine.resolved_at = now
            quarantine.resolved_by = x_admin_actor
            quarantine.resolution = "release"
            quarantine.resolution_reason = payload.reason
            session.add(
                ScreeningQuarantineResolution(
                    resolution_id=uuid4(),
                    quarantine_id=quarantine.quarantine_id,
                    resolution="release",
                    reason=payload.reason,
                    actor=x_admin_actor,
                    created_at=now,
                )
            )
        dispute.status = "resolved"
        dispute.resolved_at = now
        dispute.resolved_by = x_admin_actor
        dispute.resolution = payload.resolution
        dispute.resolution_reason = payload.reason

    history = await _resolution_history(session, [dispute.quarantine_id])
    return AdminScreeningDisputeResolveResponse(
        dispute=_dispute_item(
            dispute,
            agent,
            quarantine,
            history[dispute.quarantine_id],
        ),
        agent_status=agent.status,
    )


async def _screening_attempts_by_agent(
    session: AsyncSession, agent_ids: list[UUID]
) -> dict[UUID, list[AdminScreeningAttempt]]:
    attempts_by_agent: dict[UUID, list[AdminScreeningAttempt]] = defaultdict(list)
    if not agent_ids:
        return attempts_by_agent
    attempts = (
        (
            await session.execute(
                select(ScreeningAttempt)
                .where(ScreeningAttempt.agent_id.in_(agent_ids))
                .order_by(
                    ScreeningAttempt.started_at.desc(),
                    ScreeningAttempt.attempt_id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    duplicate_ids = {
        attempt.duplicate_of for attempt in attempts if attempt.duplicate_of is not None
    }
    duplicate_agents = {
        duplicate.agent_id: duplicate
        for duplicate in await session.scalars(
            select(Agent).where(Agent.agent_id.in_(duplicate_ids))
        )
    }
    for attempt in attempts:
        duplicate = (
            duplicate_agents.get(attempt.duplicate_of)
            if attempt.duplicate_of is not None
            else None
        )
        attempts_by_agent[attempt.agent_id].append(
            AdminScreeningAttempt(
                attempt_id=attempt.attempt_id,
                policy_version=attempt.policy_version,
                status=attempt.status,  # type: ignore[arg-type]
                screener_hotkey=attempt.screener_hotkey,
                started_at=attempt.started_at,
                deadline=attempt.deadline,
                finished_at=attempt.finished_at,
                reason=attempt.public_reason,
                reason_code=attempt.reason_code,
                duplicate_of=attempt.duplicate_of,
                duplicate_name=duplicate.name if duplicate is not None else None,
                duplicate_version=duplicate.version if duplicate is not None else None,
            )
        )
    return attempts_by_agent


def _screening_submission(
    agent: Agent,
    attempts: list[AdminScreeningAttempt],
    miner_coldkey: str | None = None,
    image_builds: list[AdminScreeningImageBuild] | None = None,
) -> AdminScreeningSubmission:
    return AdminScreeningSubmission(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        miner_coldkey=miner_coldkey,
        agent_name=agent.name,
        agent_version=agent.version,
        artifact_sha256=agent.sha256,
        agent_status=agent.status,
        screening_policy_version=agent.screening_policy_version,
        screening_reason=agent.screening_reason,
        screening_reason_code=agent.screening_reason_code,
        submitted_at=agent.created_at,
        attempts=attempts,
        image_builds=image_builds or [],
    )


@router.get("/screening-submissions", response_model=AdminScreeningSubmissionList)
async def list_screening_submissions(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminScreeningSubmissionList:
    """Return private screening history without source or artifact URLs."""
    total = int((await session.scalar(select(func.count()).select_from(Agent))) or 0)
    agents = (
        (
            await session.execute(
                select(Agent)
                .order_by(Agent.created_at.desc(), Agent.agent_id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    attempts_by_agent = await _screening_attempts_by_agent(
        session, [agent.agent_id for agent in agents]
    )
    coldkeys = await get_miner_coldkeys_for_agents(
        session, agent_ids={agent.agent_id for agent in agents}
    )
    return AdminScreeningSubmissionList(
        count=total,
        items=[
            _screening_submission(
                agent,
                attempts_by_agent[agent.agent_id],
                coldkeys.get(agent.agent_id),
            )
            for agent in agents
        ],
    )


@router.get("/screening-failures", response_model=AdminScreeningFailureSummary)
async def summarize_screening_failures(
    _admin: AdminDep,
    session: SessionDep,
    example_limit: Annotated[int, Query(ge=1, le=10)] = 3,
) -> AdminScreeningFailureSummary:
    """Group currently screening / screening_failed agents by reason_code."""
    agents = (
        (
            await session.execute(
                select(Agent)
                .where(
                    Agent.status.in_(
                        (AgentStatus.SCREENING, AgentStatus.SCREENING_FAILED)
                    )
                )
                .order_by(Agent.created_at.desc(), Agent.agent_id.desc())
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[tuple[str, str | None], list[Agent]] = defaultdict(list)
    screening = 0
    screening_failed = 0
    for agent in agents:
        if agent.status == AgentStatus.SCREENING:
            screening += 1
        else:
            screening_failed += 1
        grouped[(agent.status, agent.screening_reason_code)].append(agent)
    groups = [
        AdminScreeningFailureGroup(
            agent_status=status,
            reason_code=reason_code,
            count=len(rows),
            examples=[
                AdminScreeningFailureExample(
                    agent_id=row.agent_id,
                    agent_name=row.name,
                    agent_version=row.version,
                    agent_status=row.status,
                    submitted_at=row.created_at,
                )
                for row in rows[:example_limit]
            ],
        )
        for (status, reason_code), rows in sorted(
            grouped.items(),
            key=lambda item: (-len(item[1]), item[0][0], item[0][1] or ""),
        )
    ]
    return AdminScreeningFailureSummary(
        generated_at=datetime.now(UTC),
        screening=screening,
        screening_failed=screening_failed,
        groups=groups,
    )


@router.get(
    "/screening-submissions/{agent_id}", response_model=AdminScreeningSubmission
)
async def get_screening_submission(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminScreeningSubmission:
    """Return one exact submission and its full history, without source access."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="screening submission not found")
    attempts_by_agent = await _screening_attempts_by_agent(session, [agent_id])
    coldkey = await get_miner_coldkey_for_agent(session, agent_id=agent_id)
    builds = (
        (
            await session.execute(
                select(SubmissionImageBuild)
                .where(SubmissionImageBuild.agent_id == agent_id)
                .order_by(
                    SubmissionImageBuild.created_at.desc(),
                    SubmissionImageBuild.build_id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    image_builds = [
        AdminScreeningImageBuild(
            build_id=row.build_id,
            attempt_id=row.attempt_id,
            status=row.status,
            error_code=row.error_code,
            provider=row.provider,
            provider_resource_id=row.provider_resource_id,
            runtime_status=row.runtime_status,
            runtime_error_code=row.runtime_error_code,
            runtime_provider_resource_id=row.runtime_provider_resource_id,
            attempt_count=row.attempt_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )
        for row in builds
    ]
    return _screening_submission(
        agent, attempts_by_agent[agent_id], coldkey, image_builds
    )


@router.post(
    "/screening-submissions/{agent_id}/rescreen",
    response_model=AdminScreeningRescreenResponse,
)
async def rescreen_rejected_submission(
    agent_id: UUID,
    payload: AdminScreeningRescreenRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminScreeningRescreenResponse:
    """Return one rejected submission to the queue without rewriting history."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    async with session.begin():
        agent = await session.scalar(
            select(Agent).where(Agent.agent_id == agent_id).with_for_update()
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if agent.sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact identity changed")
        score_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(Score.agent_id == agent_id)
            )
            or 0
        )
        if score_count != payload.expected_score_count:
            raise HTTPException(status_code=409, detail="score count changed")
        running_attempt = await session.scalar(
            select(ScreeningAttempt.attempt_id).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.status == "running",
            )
        )
        if running_attempt is not None:
            raise HTTPException(status_code=409, detail="screening attempt is active")
        if agent.status != AgentStatus.REJECTED:
            raise HTTPException(status_code=409, detail="submission is not rejected")
        agent.status = AgentStatus.SCREENING_FAILED
        agent.screening_reason = "Operator requested a screening retry"
    logger.info(
        "admin_actor=%s requested rescreen agent_id=%s reason=%s",
        x_admin_actor,
        agent_id,
        payload.reason,
    )
    return AdminScreeningRescreenResponse(
        agent_id=agent_id, agent_status=AgentStatus.SCREENING_FAILED
    )


@router.post(
    "/screening-submissions/{agent_id}/retry-now",
    response_model=AdminScreeningRetryNowResponse,
)
async def retry_failed_screening_now(
    agent_id: UUID,
    payload: AdminScreeningRetryNowRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminScreeningRetryNowResponse:
    """Waive only one exact failed attempt's remaining automatic backoff."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    now = datetime.now(UTC)
    idempotent = False
    async with session.begin():
        agent = await session.scalar(
            select(Agent).where(Agent.agent_id == agent_id).with_for_update()
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if agent.sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact identity changed")
        score_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(Score.agent_id == agent_id)
            )
            or 0
        )
        if score_count != payload.expected_score_count:
            raise HTTPException(status_code=409, detail="score count changed")
        attempt = await session.scalar(
            select(ScreeningAttempt)
            .where(
                ScreeningAttempt.attempt_id == payload.expected_attempt_id,
                ScreeningAttempt.agent_id == agent_id,
            )
            .with_for_update()
        )
        if attempt is None:
            raise HTTPException(status_code=409, detail="screening attempt changed")
        latest_attempt_id = await session.scalar(
            select(ScreeningAttempt.attempt_id)
            .where(ScreeningAttempt.agent_id == agent_id)
            .order_by(
                ScreeningAttempt.started_at.desc(),
                ScreeningAttempt.attempt_id.desc(),
            )
            .limit(1)
        )
        if latest_attempt_id != attempt.attempt_id:
            raise HTTPException(status_code=409, detail="screening attempt changed")
        override = await session.scalar(
            select(ScreeningRetryOverride).where(
                ScreeningRetryOverride.attempt_id == attempt.attempt_id
            )
        )
        if override is not None:
            idempotent = True
        else:
            running_attempt = await session.scalar(
                select(ScreeningAttempt.attempt_id).where(
                    ScreeningAttempt.agent_id == agent_id,
                    ScreeningAttempt.status == "running",
                )
            )
            if running_attempt is not None:
                raise HTTPException(
                    status_code=409, detail="screening attempt is active"
                )
            if agent.status != AgentStatus.SCREENING_FAILED:
                raise HTTPException(
                    status_code=409,
                    detail="submission is not waiting after a failed screening",
                )
            if attempt.status != "expired":
                raise HTTPException(
                    status_code=409, detail="screening attempt is not expired"
                )
            if attempt.deadline <= now:
                raise HTTPException(
                    status_code=409, detail="screening backoff has already elapsed"
                )
            override = ScreeningRetryOverride(
                override_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt.attempt_id,
                artifact_sha256=agent.sha256,
                expected_score_count=score_count,
                reason=payload.reason,
                actor=x_admin_actor,
                created_at=now,
            )
            session.add(override)
            await session.flush()
    logger.info(
        "admin_actor=%s waived screening backoff agent_id=%s attempt_id=%s "
        "override_id=%s idempotent=%s reason=%s",
        x_admin_actor,
        agent_id,
        attempt.attempt_id,
        override.override_id,
        idempotent,
        payload.reason,
    )
    return AdminScreeningRetryNowResponse(
        override_id=override.override_id,
        agent_id=agent_id,
        attempt_id=attempt.attempt_id,
        agent_status=agent.status,
        backoff_deadline=attempt.deadline,
        created_at=override.created_at,
        idempotent=idempotent,
    )


@router.post(
    "/screening-submissions/{agent_id}/expire-running",
    response_model=AdminExpireRunningScreeningResponse,
)
async def expire_running_screening(
    agent_id: UUID,
    payload: AdminExpireRunningScreeningRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminExpireRunningScreeningResponse:
    """Expire one live screening attempt so the queue can admit a replacement."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    now = datetime.now(UTC)
    expired_build_ids: list[UUID] = []
    async with session.begin():
        agent = await session.scalar(
            select(Agent).where(Agent.agent_id == agent_id).with_for_update()
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if agent.sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact identity changed")
        score_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(Score.agent_id == agent_id)
            )
            or 0
        )
        if score_count != payload.expected_score_count:
            raise HTTPException(status_code=409, detail="score count changed")
        attempt = await session.scalar(
            select(ScreeningAttempt)
            .where(
                ScreeningAttempt.attempt_id == payload.expected_attempt_id,
                ScreeningAttempt.agent_id == agent_id,
            )
            .with_for_update()
        )
        if attempt is None:
            raise HTTPException(status_code=409, detail="screening attempt changed")
        if (
            attempt.status == "failed"
            and attempt.reason_code == "operator-expired-running-screening"
        ):
            return AdminExpireRunningScreeningResponse(
                agent_id=agent_id,
                attempt_id=attempt.attempt_id,
                agent_status=agent.status,
                expired_build_ids=[],
                idempotent=True,
            )
        if attempt.status != "running":
            raise HTTPException(
                status_code=409, detail="screening attempt is not running"
            )
        if agent.status != AgentStatus.SCREENING:
            raise HTTPException(status_code=409, detail="submission is not screening")
        attempt.status = "failed"
        attempt.finished_at = now
        attempt.public_reason = payload.reason
        attempt.reason_code = "operator-expired-running-screening"
        builds = list(
            await session.scalars(
                select(SubmissionImageBuild)
                .where(
                    SubmissionImageBuild.attempt_id == attempt.attempt_id,
                    SubmissionImageBuild.status.in_(("queued", "leased", "running")),
                )
                .with_for_update()
            )
        )
        for build in builds:
            build.status = "fallback_required"
            build.error_code = "OPERATOR_SCREENING_EXPIRED"
            build.completed_at = now
            build.updated_at = now
            build.lease_expires_at = None
            expired_build_ids.append(build.build_id)
        reviews = list(
            await session.scalars(
                select(SubmissionSourceReview)
                .where(
                    SubmissionSourceReview.attempt_id == attempt.attempt_id,
                    SubmissionSourceReview.status.in_(("queued", "leased", "running")),
                )
                .with_for_update()
            )
        )
        for review in reviews:
            review.status = "fallback_required"
            review.error_code = "OPERATOR_SCREENING_EXPIRED"
            review.completed_at = now
            review.updated_at = now
            review.lease_expires_at = None
        agent.status = AgentStatus.SCREENING_FAILED
        agent.screening_reason = payload.reason
        agent.screening_reason_code = "operator-expired-running-screening"
    logger.info(
        "admin_actor=%s expired running screening agent_id=%s attempt_id=%s "
        "builds=%s reason=%s",
        x_admin_actor,
        agent_id,
        attempt.attempt_id,
        expired_build_ids,
        payload.reason,
    )
    return AdminExpireRunningScreeningResponse(
        agent_id=agent_id,
        attempt_id=attempt.attempt_id,
        agent_status=AgentStatus.SCREENING_FAILED,
        expired_build_ids=expired_build_ids,
        idempotent=False,
    )


async def _screened_image_rebuild_detail(
    session: AsyncSession, *, agent_id: UUID
) -> AdminScreenedImageRebuildDetail:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    bench_version = await active_bench_version(session)
    score_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Score)
            .where(
                Score.agent_id == agent_id,
                Score.bench_version == bench_version,
            )
        )
        or 0
    )
    screening_attempt_active = (
        await session.scalar(
            select(ScreeningAttempt.attempt_id).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.status == "running",
            )
        )
        is not None
    )
    validator_ticket_active = (
        await session.scalar(
            select(ValidatorTicket.agent_id).where(
                ValidatorTicket.agent_id == agent_id,
                ValidatorTicket.bench_version == bench_version,
                ValidatorTicket.status == TicketStatus.ISSUED,
            )
        )
        is not None
    )
    blocking_reason: str | None = None
    if bench_version <= 2:
        blocking_reason = "active benchmark does not use screened images"
    elif agent.status != AgentStatus.EVALUATING:
        blocking_reason = "submission is not waiting for validator scores"
    elif score_count != 0:
        blocking_reason = "submission already has an accepted active-version score"
    elif screening_attempt_active:
        blocking_reason = "screening attempt is active"
    elif agent.screening_policy_version < SCREENING_POLICY_VERSION:
        blocking_reason = "submission is not on the current screening policy"
    elif agent.screened_image_sha256 is None or agent.screened_image_upload_id is None:
        blocking_reason = "submission has no complete screened image to replace"
    return AdminScreenedImageRebuildDetail(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        agent_status=agent.status.value,
        artifact_sha256=agent.sha256,
        bench_version=bench_version,
        score_count=score_count,
        screened_image_sha256=agent.screened_image_sha256,
        screened_image_upload_id=agent.screened_image_upload_id,
        screening_attempt_active=screening_attempt_active,
        validator_ticket_active=validator_ticket_active,
        rebuild_allowed=blocking_reason is None,
        blocking_reason=blocking_reason,
    )


@router.get(
    "/screening-submissions/{agent_id}/rebuild-screened-image",
    response_model=AdminScreenedImageRebuildDetail,
)
async def inspect_screened_image_rebuild(
    agent_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminScreenedImageRebuildDetail:
    """Return exact guards for a build-only screened-image repair."""
    return await _screened_image_rebuild_detail(session, agent_id=agent_id)


@router.post(
    "/screening-submissions/{agent_id}/rebuild-screened-image",
    response_model=AdminScreenedImageRebuildResponse,
)
async def rebuild_screened_image(
    agent_id: UUID,
    payload: AdminScreenedImageRebuildRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminScreenedImageRebuildResponse:
    """Rebuild only an accepted submission's stale image transport.

    Clearing the atomic image identity makes the existing screening queue claim
    this current-policy EVALUATING submission in its fail-closed ``build_only``
    mode. Source review and the accepted screening verdict are preserved.
    """
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    now = datetime.now(UTC)
    async with session.begin():
        agent = await session.scalar(
            select(Agent).where(Agent.agent_id == agent_id).with_for_update()
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        detail = await _screened_image_rebuild_detail(session, agent_id=agent_id)
        if not detail.rebuild_allowed:
            raise HTTPException(status_code=409, detail=detail.blocking_reason)
        if agent.sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact identity changed")
        if detail.bench_version != payload.expected_bench_version:
            raise HTTPException(status_code=409, detail="active benchmark changed")
        if detail.score_count != payload.expected_score_count:
            raise HTTPException(status_code=409, detail="score count changed")
        if agent.screened_image_sha256 != payload.expected_image_sha256:
            raise HTTPException(status_code=409, detail="screened image changed")
        if agent.screened_image_upload_id != payload.expected_image_upload_id:
            raise HTTPException(status_code=409, detail="screened image upload changed")

        tickets = list(
            await session.scalars(
                select(ValidatorTicket)
                .where(
                    ValidatorTicket.agent_id == agent_id,
                    ValidatorTicket.bench_version == detail.bench_version,
                    ValidatorTicket.status != TicketStatus.SCORED,
                )
                .with_for_update()
            )
        )
        for ticket in tickets:
            ticket.status = TicketStatus.EXPIRED
            ticket.deadline = now
            ticket.retry_after = now
            ticket.manual_retry_grants += max(
                1, ticket.attempt_count - ticket_attempt_cap(ticket) + 1
            )

        old_image_sha256 = agent.screened_image_sha256
        old_upload_id = agent.screened_image_upload_id
        agent.screened_image_sha256 = None
        agent.screened_image_size_bytes = None
        agent.screened_image_id = None
        agent.screened_image_ref = None
        agent.screened_image_upload_id = None
        agent.screened_image_verified_at = None
        agent.screening_reason = "Operator requested screened image rebuild"
        agent.screening_reason_code = None
        await append_audit_entry(
            session,
            agent_id=agent_id,
            validator_hotkey=None,
            event=screened_image_rebuild_event(detail.bench_version),
            payload={
                "bench_version": detail.bench_version,
                "prior_image_sha256": old_image_sha256,
                "prior_image_upload_id": str(old_upload_id),
            },
            recorded_at=now,
        )

    logger.warning(
        "admin_actor=%s requested screened-image rebuild agent_id=%s "
        "bench_version=%s expired_tickets=%s reason=%s",
        x_admin_actor,
        agent_id,
        detail.bench_version,
        len(tickets),
        payload.reason,
    )
    return AdminScreenedImageRebuildResponse(
        agent_id=agent_id,
        agent_status=AgentStatus.EVALUATING.value,
        bench_version=detail.bench_version,
        expired_ticket_count=len(tickets),
    )


@router.post(
    "/screening-submissions/{agent_id}/refresh-benchmark-contract",
    response_model=AdminBenchmarkContractRefreshResponse,
)
async def refresh_benchmark_contract(
    agent_id: UUID,
    payload: AdminBenchmarkContractRefreshRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminBenchmarkContractRefreshResponse:
    """Rebuild one stale v3+ dataset and screened image before ticketing again.

    This is an operator-only recovery path for a dataset-generator/scorer drift.
    It preserves submission and score history, but only permits the repair when
    the active benchmark has exactly the expected number of accepted scores.
    """
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")

    now = datetime.now(UTC)
    async with session.begin():
        agent = await session.scalar(
            select(Agent).where(Agent.agent_id == agent_id).with_for_update()
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if agent.sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact identity changed")

        bench_version = await active_bench_version(session)
        if bench_version != payload.expected_bench_version:
            raise HTTPException(status_code=409, detail="active benchmark changed")
        dataset = await session.get(BenchmarkDataset, (agent_id, bench_version))
        if dataset is None:
            raise HTTPException(status_code=409, detail="benchmark dataset is missing")
        if dataset.sha256 != payload.expected_dataset_sha256:
            raise HTTPException(status_code=409, detail="benchmark dataset changed")

        score_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(
                    Score.agent_id == agent_id,
                    Score.bench_version == bench_version,
                )
            )
            or 0
        )
        if score_count != payload.expected_score_count:
            raise HTTPException(status_code=409, detail="score count changed")
        if score_count != 0:
            raise HTTPException(
                status_code=409,
                detail="benchmark contract refresh requires zero accepted scores",
            )

        running_attempt = await session.scalar(
            select(ScreeningAttempt.attempt_id).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.status == "running",
            )
        )
        if running_attempt is not None:
            raise HTTPException(status_code=409, detail="screening attempt is active")

        tickets = list(
            await session.scalars(
                select(ValidatorTicket)
                .where(
                    ValidatorTicket.agent_id == agent_id,
                    ValidatorTicket.bench_version == bench_version,
                    ValidatorTicket.status != TicketStatus.SCORED,
                )
                .with_for_update()
            )
        )
        for ticket in tickets:
            ticket.status = TicketStatus.EXPIRED
            ticket.deadline = now
            ticket.retry_after = now
            # The replacement dataset is a new contract even though its public
            # benchmark version is unchanged. Grant one clean lease without
            # erasing the historical attempt counter.
            ticket.manual_retry_grants += max(
                1, ticket.attempt_count - ticket_attempt_cap(ticket) + 1
            )

        await append_audit_entry(
            session,
            agent_id=agent_id,
            validator_hotkey=None,
            event=benchmark_contract_refresh_event(bench_version),
            payload={"bench_version": bench_version},
            recorded_at=now,
        )

        await session.execute(
            delete(BenchmarkDataset).where(
                BenchmarkDataset.agent_id == agent_id,
                BenchmarkDataset.bench_version == bench_version,
            )
        )
        agent.screened_image_sha256 = None
        agent.screened_image_size_bytes = None
        agent.screened_image_id = None
        agent.screened_image_ref = None
        agent.screened_image_upload_id = None
        agent.screened_image_verified_at = None
        agent.status = AgentStatus.SCREENING_FAILED
        agent.screening_reason = "Operator requested benchmark contract rebuild"
        agent.screening_reason_code = None

    logger.warning(
        "admin_actor=%s refreshed benchmark contract agent_id=%s "
        "bench_version=%s expired_tickets=%s reason=%s",
        x_admin_actor,
        agent_id,
        bench_version,
        len(tickets),
        payload.reason,
    )
    return AdminBenchmarkContractRefreshResponse(
        agent_id=agent_id,
        agent_status=AgentStatus.SCREENING_FAILED,
        bench_version=bench_version,
        expired_ticket_count=len(tickets),
    )


@router.get(
    "/screening-submissions/{agent_id}/refresh-benchmark-contract",
    response_model=AdminBenchmarkContractRefreshDetail,
)
async def inspect_benchmark_contract_refresh(
    agent_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminBenchmarkContractRefreshDetail:
    """Return the exact guarded inputs Backroom must confirm before repair."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    bench_version = await active_bench_version(session)
    dataset = await session.get(BenchmarkDataset, (agent_id, bench_version))
    score_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Score)
            .where(
                Score.agent_id == agent_id,
                Score.bench_version == bench_version,
            )
        )
        or 0
    )
    screening_attempt_active = (
        await session.scalar(
            select(ScreeningAttempt.attempt_id).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.status == "running",
            )
        )
        is not None
    )
    blocking_reason: str | None = None
    if bench_version <= 2:
        blocking_reason = "active benchmark does not support contract refresh"
    elif dataset is None:
        blocking_reason = "benchmark dataset is missing"
    elif score_count != 0:
        blocking_reason = "submission already has an accepted active-version score"
    elif screening_attempt_active:
        blocking_reason = "screening attempt is active"

    return AdminBenchmarkContractRefreshDetail(
        agent_id=agent_id,
        agent_name=agent.name,
        agent_status=agent.status,
        artifact_sha256=agent.sha256,
        bench_version=bench_version,
        dataset_sha256=dataset.sha256 if dataset is not None else None,
        score_count=score_count,
        screening_attempt_active=screening_attempt_active,
        refresh_allowed=blocking_reason is None,
        blocking_reason=blocking_reason,
    )


async def _benchmark_contract_migration_state(
    session: AsyncSession, *, agent_id: UUID
) -> tuple[
    Agent,
    BenchmarkRollout | None,
    BenchmarkDataset | None,
    BenchmarkDataset | None,
    int,
    int,
    bool,
    bool,
]:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    rollout = await open_rollout(session)
    target_version = rollout.desired_version if rollout is not None else None
    source = await session.get(BenchmarkDataset, (agent_id, 2))
    target = (
        await session.get(BenchmarkDataset, (agent_id, target_version))
        if target_version is not None
        else None
    )
    source_scores = int(
        await session.scalar(
            select(func.count())
            .select_from(Score)
            .where(Score.agent_id == agent_id, Score.bench_version == 2)
        )
        or 0
    )
    target_scores = int(
        await session.scalar(
            select(func.count())
            .select_from(Score)
            .where(
                Score.agent_id == agent_id,
                Score.bench_version == (target_version or -1),
            )
        )
        or 0
    )
    screening_active = (
        await session.scalar(
            select(ScreeningAttempt.attempt_id).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.status == "running",
            )
        )
        is not None
    )
    validator_active = (
        await session.scalar(
            select(ValidatorHeartbeat.validator_hotkey).where(
                ValidatorHeartbeat.active_agent_id == agent_id,
                ValidatorHeartbeat.state == "running_benchmark",
                ValidatorHeartbeat.seen_at >= datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        is not None
    )
    return (
        agent,
        rollout,
        source,
        target,
        source_scores,
        target_scores,
        screening_active,
        validator_active,
    )


async def _benchmark_qualification_state(
    session: AsyncSession,
    *,
    agent_id: UUID,
    generator_run_size: str | None,
    for_update: bool = False,
) -> tuple[
    AdminBenchmarkQualificationDetail,
    PendingQualification | None,
]:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    rollout = await open_rollout(session)
    source_version = rollout.from_version if rollout is not None else None
    target_version = rollout.desired_version if rollout is not None else None
    total_scores = int(
        await session.scalar(
            select(func.count()).select_from(Score).where(Score.agent_id == agent_id)
        )
        or 0
    )
    source_scores = (
        int(
            await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(
                    Score.agent_id == agent_id,
                    Score.bench_version == source_version,
                )
            )
            or 0
        )
        if source_version is not None
        else 0
    )
    target_scores = (
        int(
            await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(
                    Score.agent_id == agent_id,
                    Score.bench_version == target_version,
                )
            )
            or 0
        )
        if target_version is not None
        else 0
    )
    screening_active = (
        await session.scalar(
            select(ScreeningAttempt.attempt_id).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.status == "running",
            )
        )
        is not None
    )
    now = datetime.now(UTC)
    issued_ticket_statement = select(ValidatorTicket.agent_id).where(
        ValidatorTicket.agent_id == agent_id,
        ValidatorTicket.status == TicketStatus.ISSUED,
        ValidatorTicket.deadline > now,
    )
    if for_update:
        issued_ticket_statement = issued_ticket_statement.with_for_update()
    issued_ticket_active = (
        await session.scalar(issued_ticket_statement.limit(1)) is not None
    )
    heartbeat_active = (
        await session.scalar(
            select(ValidatorHeartbeat.validator_hotkey).where(
                ValidatorHeartbeat.active_agent_id == agent_id,
                ValidatorHeartbeat.state == "running_benchmark",
                ValidatorHeartbeat.seen_at >= now - timedelta(minutes=5),
            )
        )
        is not None
    )
    validator_active = issued_ticket_active or heartbeat_active
    top_five = (
        # The rollout's own frozen target, not the live policy: this answers
        # "would quarantining this agent disturb the open rollout's cohort?",
        # and that cohort is the size the rollout froze at start.
        await historical_rescore_cohort(
            session,
            source_version=rollout.from_version,
            limit=rollout.rescore_cohort_target,
        )
        if rollout is not None
        else []
    )
    top_member = next(
        (member for member in top_five if member.agent_id == agent_id), None
    )
    member = (
        await session.get(BenchmarkRolloutMember, (rollout.rollout_id, agent_id))
        if rollout is not None
        else None
    )
    target_dataset = (
        await session.get(BenchmarkDataset, (agent_id, target_version))
        if target_version is not None
        else None
    )
    candidate = None
    candidate_reason = None
    if rollout is not None and top_member is not None:
        candidate, candidate_reason = await qualification_candidate(
            session,
            source_bench_version=rollout.from_version,
            target_bench_version=rollout.desired_version,
            member=top_member,
            generator_run_size=generator_run_size,
        )
    blocking_reason: str | None = None
    if rollout is None:
        blocking_reason = "an open benchmark rollout is required"
    elif agent.status not in (AgentStatus.SCORED, AgentStatus.LIVE):
        blocking_reason = "submission must be scored or live"
    elif top_member is None:
        blocking_reason = "submission is not in the inherited top-ten cohort"
    elif member is not None:
        blocking_reason = "submission is already a rollout member"
    elif screening_active:
        blocking_reason = "screening attempt is active"
    elif validator_active:
        blocking_reason = "validator benchmark is active"
    elif candidate is None:
        blocking_reason = candidate_reason or "dataset input is unavailable"
    detail = AdminBenchmarkQualificationDetail(
        agent_id=agent_id,
        agent_name=agent.name,
        agent_status=agent.status,
        artifact_sha256=agent.sha256,
        rollout_id=rollout.rollout_id if rollout is not None else None,
        source_bench_version=source_version,
        target_bench_version=target_version,
        currently_top_five=top_member is not None,
        rollout_member=member is not None,
        target_dataset_sha256=(
            target_dataset.sha256 if target_dataset is not None else None
        ),
        total_score_count=total_scores,
        source_score_count=source_scores,
        target_score_count=target_scores,
        screening_attempt_active=screening_active,
        validator_run_active=validator_active,
        qualification_allowed=blocking_reason is None,
        blocking_reason=blocking_reason,
    )
    return detail, candidate


@router.get(
    "/screening-submissions/{agent_id}/qualify-benchmark-rollout",
    response_model=AdminBenchmarkQualificationDetail,
)
async def inspect_benchmark_qualification(
    agent_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
    generator: GeneratorDep,
) -> AdminBenchmarkQualificationDetail:
    """Inspect the guarded scored/live rolling-qualification inputs."""
    detail, _candidate = await _benchmark_qualification_state(
        session,
        agent_id=agent_id,
        generator_run_size=generator.run_size,
    )
    return detail


@router.post(
    "/screening-submissions/{agent_id}/qualify-benchmark-rollout",
    response_model=AdminBenchmarkQualificationResponse,
)
async def qualify_benchmark_rollout(
    agent_id: UUID,
    payload: AdminBenchmarkQualificationRequest,
    _admin: AdminDep,
    session: SessionDep,
    generator: GeneratorDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
    request: Request = None,  # type: ignore[assignment]
) -> AdminBenchmarkQualificationResponse:
    """Append a guarded cohort member without touching its accepted scores."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")

    detail, candidate = await _benchmark_qualification_state(
        session,
        agent_id=agent_id,
        generator_run_size=generator.run_size,
    )
    if detail.rollout_id != payload.expected_rollout_id:
        raise HTTPException(status_code=409, detail="open benchmark rollout changed")
    if detail.artifact_sha256 != payload.expected_sha256:
        raise HTTPException(status_code=409, detail="artifact identity changed")
    if (
        detail.total_score_count != payload.expected_total_score_count
        or detail.source_score_count != payload.expected_source_score_count
        or detail.target_score_count != payload.expected_target_score_count
    ):
        raise HTTPException(status_code=409, detail="score count changed")
    if not detail.qualification_allowed or candidate is None:
        raise HTTPException(
            status_code=409,
            detail=detail.blocking_reason or "qualification is not allowed",
        )
    await session.rollback()
    target_version = detail.target_bench_version
    assert target_version is not None
    target_sha256 = candidate.existing_sha256 or await generator.generate(
        candidate.seed, bench_version=target_version
    )

    async with session.begin():
        locked_agent = await session.scalar(
            select(Agent).where(Agent.agent_id == agent_id).with_for_update()
        )
        if locked_agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        locked_rollout = await open_rollout(session, for_update=True)
        if (
            locked_rollout is None
            or locked_rollout.rollout_id != payload.expected_rollout_id
        ):
            raise HTTPException(
                status_code=409, detail="open benchmark rollout changed"
            )
        current, current_candidate = await _benchmark_qualification_state(
            session,
            agent_id=agent_id,
            generator_run_size=generator.run_size,
            for_update=True,
        )
        if current.artifact_sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact identity changed")
        if (
            current.total_score_count != payload.expected_total_score_count
            or current.source_score_count != payload.expected_source_score_count
            or current.target_score_count != payload.expected_target_score_count
        ):
            raise HTTPException(status_code=409, detail="score count changed")
        if not current.qualification_allowed or current_candidate is None:
            raise HTTPException(
                status_code=409,
                detail=current.blocking_reason or "qualification is not allowed",
            )
        if current_candidate != candidate:
            raise HTTPException(status_code=409, detail="dataset input changed")
        appended = await append_rollout_member(
            session,
            rollout=locked_rollout,
            member=current_candidate.member,
            dataset=RolloutDatasetPin(
                seed=current_candidate.seed,
                sha256=target_sha256,
                run_size=current_candidate.run_size,
                seed_block=current_candidate.seed_block,
                seed_block_hash=current_candidate.seed_block_hash,
            ),
            now=datetime.now(UTC),
            audit_context={
                "origin": "manual",
                "actor": x_admin_actor,
                "reason": payload.reason,
                "seed_source": current_candidate.seed_source,
            },
        )
        if not appended:
            raise HTTPException(status_code=409, detail="qualification changed")
        activation_now = datetime.now(UTC)
        await maybe_activate_rollout(
            session,
            locked_rollout,
            now=activation_now,
            inference_requirements=inference_activation_requirements(
                (
                    request.app.state.config.inference_proxy
                    if request is not None
                    else None
                ),
                bench_version=locked_rollout.desired_version,
            ),
        )
        screening_queued = (
            locked_agent.screening_policy_version
            < benchmark_contract(target_version).minimum_screening_policy_version
            or locked_agent.screened_image_sha256 is None
            or locked_agent.screened_image_size_bytes is None
            or locked_agent.screened_image_id is None
            or locked_agent.screened_image_ref is None
            or locked_agent.screened_image_upload_id is None
            or locked_agent.screened_image_verified_at is None
        )

    logger.warning(
        "admin_actor=%s qualified rolling contender agent_id=%s rollout_id=%s "
        "target_version=%s screening_queued=%s reason=%s",
        x_admin_actor,
        agent_id,
        payload.expected_rollout_id,
        target_version,
        screening_queued,
        payload.reason,
    )
    return AdminBenchmarkQualificationResponse(
        agent_id=agent_id,
        agent_status=locked_agent.status,
        rollout_id=payload.expected_rollout_id,
        target_bench_version=target_version,
        target_dataset_sha256=target_sha256,
        screening_queued=screening_queued,
    )


@router.get(
    "/screening-submissions/{agent_id}/migrate-benchmark-contract",
    response_model=AdminBenchmarkContractMigrationDetail,
)
async def inspect_benchmark_contract_migration(
    agent_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminBenchmarkContractMigrationDetail:
    """Inspect the guarded zero-score v2-to-v3 migration inputs."""
    (
        agent,
        rollout,
        source,
        target,
        source_scores,
        target_scores,
        screening_active,
        validator_active,
    ) = await _benchmark_contract_migration_state(session, agent_id=agent_id)
    blocking_reason: str | None = None
    if rollout is None or rollout.from_version != 2 or rollout.desired_version != 3:
        blocking_reason = "an open v2-to-v3 rollout is required"
    elif source is None:
        blocking_reason = "source v2 dataset is missing"
    elif target is not None:
        blocking_reason = "target v3 dataset already exists"
    elif source_scores != 0 or target_scores != 0:
        blocking_reason = "migration requires zero accepted v2 and v3 scores"
    elif screening_active:
        blocking_reason = "screening attempt is active"
    elif validator_active:
        blocking_reason = "validator benchmark is active"
    return AdminBenchmarkContractMigrationDetail(
        agent_id=agent_id,
        agent_name=agent.name,
        agent_status=agent.status,
        artifact_sha256=agent.sha256,
        source_bench_version=2,
        target_bench_version=rollout.desired_version if rollout is not None else None,
        source_dataset_sha256=source.sha256 if source is not None else None,
        target_dataset_sha256=target.sha256 if target is not None else None,
        source_score_count=source_scores,
        target_score_count=target_scores,
        screening_attempt_active=screening_active,
        validator_run_active=validator_active,
        migration_allowed=blocking_reason is None,
        blocking_reason=blocking_reason,
    )


@router.post(
    "/screening-submissions/{agent_id}/migrate-benchmark-contract",
    response_model=AdminBenchmarkContractMigrationResponse,
)
async def migrate_benchmark_contract(
    agent_id: UUID,
    payload: AdminBenchmarkContractMigrationRequest,
    _admin: AdminDep,
    session: SessionDep,
    generator: GeneratorDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminBenchmarkContractMigrationResponse:
    """Preserve a zero-score v2 submission while rebuilding it for v3."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")

    async with session.begin():
        state = await _benchmark_contract_migration_state(session, agent_id=agent_id)
        (
            agent,
            rollout,
            source,
            target,
            source_scores,
            target_scores,
            screening_active,
            validator_active,
        ) = state
        if rollout is None or rollout.from_version != 2 or rollout.desired_version != 3:
            raise HTTPException(status_code=409, detail="open v2-to-v3 rollout changed")
        if source is None:
            raise HTTPException(status_code=409, detail="source v2 dataset is missing")
        if source.sha256 != payload.expected_source_dataset_sha256:
            raise HTTPException(status_code=409, detail="source v2 dataset changed")
        if agent.sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact identity changed")
        if target is not None:
            raise HTTPException(
                status_code=409, detail="target v3 dataset already exists"
            )
        if source_scores != 0 or target_scores != 0:
            raise HTTPException(status_code=409, detail="score count changed")
        if screening_active:
            raise HTTPException(status_code=409, detail="screening attempt is active")
        if validator_active:
            raise HTTPException(status_code=409, detail="validator benchmark is active")
        source_pin = (
            source.seed,
            source.run_size,
            source.seed_block,
            source.seed_block_hash,
        )

    target_sha256 = await generator.generate(source_pin[0], bench_version=3)
    now = datetime.now(UTC)
    async with session.begin():
        # Fresh name: `agent` above is the (non-Optional) pre-check read; this is
        # the locked re-read that the mutation below must go through.
        locked_agent = await session.scalar(
            select(Agent).where(Agent.agent_id == agent_id).with_for_update()
        )
        if locked_agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if locked_agent.sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact identity changed")
        rollout = await open_rollout(session, for_update=True)
        if rollout is None or rollout.from_version != 2 or rollout.desired_version != 3:
            raise HTTPException(status_code=409, detail="open v2-to-v3 rollout changed")
        source = await session.get(
            BenchmarkDataset, (agent_id, 2), with_for_update=True
        )
        if source is None or source.sha256 != payload.expected_source_dataset_sha256:
            raise HTTPException(status_code=409, detail="source v2 dataset changed")
        if source_pin != (
            source.seed,
            source.run_size,
            source.seed_block,
            source.seed_block_hash,
        ):
            raise HTTPException(status_code=409, detail="source v2 dataset changed")
        if await session.get(BenchmarkDataset, (agent_id, 3)) is not None:
            raise HTTPException(
                status_code=409, detail="target v3 dataset already exists"
            )
        score_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(Score.agent_id == agent_id, Score.bench_version.in_((2, 3)))
            )
            or 0
        )
        if score_count != 0:
            raise HTTPException(status_code=409, detail="score count changed")
        if (
            await session.scalar(
                select(ScreeningAttempt.attempt_id).where(
                    ScreeningAttempt.agent_id == agent_id,
                    ScreeningAttempt.status == "running",
                )
            )
            is not None
        ):
            raise HTTPException(status_code=409, detail="screening attempt is active")
        if (
            await session.scalar(
                select(ValidatorHeartbeat.validator_hotkey).where(
                    ValidatorHeartbeat.active_agent_id == agent_id,
                    ValidatorHeartbeat.state == "running_benchmark",
                    ValidatorHeartbeat.seen_at >= now - timedelta(minutes=5),
                )
            )
            is not None
        ):
            raise HTTPException(status_code=409, detail="validator benchmark is active")

        tickets = list(
            await session.scalars(
                select(ValidatorTicket)
                .where(
                    ValidatorTicket.agent_id == agent_id,
                    ValidatorTicket.bench_version.in_((2, 3)),
                    ValidatorTicket.status != TicketStatus.SCORED,
                )
                .with_for_update()
            )
        )
        for ticket in tickets:
            ticket.status = TicketStatus.EXPIRED
            ticket.deadline = now
            ticket.retry_after = now
        session.add(
            BenchmarkDataset(
                agent_id=agent_id,
                bench_version=3,
                seed=source.seed,
                sha256=target_sha256,
                run_size=source.run_size,
                seed_block=source.seed_block,
                seed_block_hash=source.seed_block_hash,
                created_at=now,
            )
        )
        locked_agent.screened_image_sha256 = None
        locked_agent.screened_image_size_bytes = None
        locked_agent.screened_image_id = None
        locked_agent.screened_image_ref = None
        locked_agent.screened_image_upload_id = None
        locked_agent.screened_image_verified_at = None
        locked_agent.status = AgentStatus.SCREENING_FAILED
        locked_agent.screening_reason = (
            "Operator migrated zero-score benchmark contract from v2 to v3"
        )
        locked_agent.screening_reason_code = None

    logger.warning(
        "admin_actor=%s migrated zero-score benchmark contract agent_id=%s "
        "source_version=2 target_version=3 expired_tickets=%s reason=%s",
        x_admin_actor,
        agent_id,
        len(tickets),
        payload.reason,
    )
    return AdminBenchmarkContractMigrationResponse(
        agent_id=agent_id,
        agent_status=AgentStatus.SCREENING_FAILED,
        source_bench_version=2,
        target_bench_version=3,
        target_dataset_sha256=target_sha256,
        expired_ticket_count=len(tickets),
    )


@router.get(
    "/screening-submissions/{agent_id}/artifact",
    response_model=ArtifactResponse,
)
async def get_screening_artifact(
    agent_id: UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> ArtifactResponse:
    """Issue an audited five-minute artifact URL to an authenticated operator."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    expires_in = 300
    url = await storage.presigned_get_url(
        key=f"{agent_id}/agent.tar.gz", expires_in=expires_in
    )
    logger.info(
        "admin_actor=%s issued screening artifact url for agent_id=%s",
        x_admin_actor,
        agent_id,
    )
    # X-Admin-Actor is unauthenticated free text behind a shared bearer token,
    # so this attributes to a claimed operator, not a proven one. Recorded as
    # the best identity the route has; the peer address corroborates it.
    await record_artifact_fetch(
        session,
        agent_id=agent_id,
        endpoint=ENDPOINT_ADMIN_SCREENING_ARTIFACT,
        requester_kind="admin",
        requester_id=x_admin_actor,
        artifact_sha256=agent.sha256,
        source_ip=client_ip(request),
        detail=request_detail(request),
    )
    return ArtifactResponse(
        agent_id=agent_id,
        sha256=agent.sha256,
        download_url=url,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )


async def _load_inspector(
    agent_id: UUID, session: AsyncSession, storage: S3StorageClient
) -> tuple[Agent, TarSourceInspector]:
    """Fetch the stored tarball, verify its digest, and open a bounded reader."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    try:
        tar_bytes = await storage.get_object(
            key=f"{agent_id}/agent.tar.gz", max_bytes=MAX_TARBALL_BYTES
        )
    except ObjectDownloadFailedError as error:
        raise HTTPException(
            status_code=502, detail="artifact is unavailable in storage"
        ) from error

    def _verify_and_open() -> TarSourceInspector:
        # Digest verification and archive characterization are CPU-bound over
        # attacker-supplied bytes; keep them off the event loop.
        if hashlib.sha256(tar_bytes).hexdigest() != agent.sha256:
            raise HTTPException(
                status_code=502, detail="stored artifact does not match its digest"
            )
        return TarSourceInspector(tar_bytes)

    try:
        return agent, await asyncio.to_thread(_verify_and_open)
    except SourceInspectError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/screening-submissions/{agent_id}/source-files",
    response_model=AdminSourceListing,
)
async def list_screening_source_files(
    agent_id: UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminSourceListing:
    """Audited, bounded file inventory of one submission tarball."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    agent, inspector = await _load_inspector(agent_id, session, storage)
    logger.info(
        "admin_actor=%s listed screening source for agent_id=%s",
        x_admin_actor,
        agent_id,
    )
    await record_artifact_fetch(
        session,
        agent_id=agent_id,
        endpoint=ENDPOINT_ADMIN_SOURCE_FILES,
        requester_kind="admin",
        requester_id=x_admin_actor,
        artifact_sha256=agent.sha256,
        source_ip=client_ip(request),
        detail=request_detail(request),
    )
    listing = await asyncio.to_thread(inspector.listing)
    return AdminSourceListing(
        agent_id=agent_id,
        artifact_sha256=agent.sha256,
        **listing,  # type: ignore[arg-type]
    )


@router.get(
    "/screening-submissions/{agent_id}/source-file",
    response_model=AdminSourceExcerpt,
)
async def read_screening_source_file(
    agent_id: UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    path: Annotated[str, Query(min_length=1, max_length=240)],
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int, Query(ge=1)] = MAX_READ_LINES,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminSourceExcerpt:
    """Audited, bounded line excerpt from one submission source file.

    Pairs with the source-review finding's ``path:line`` evidence so the
    operator can see exactly the flagged code without a full download.
    """
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    agent, inspector = await _load_inspector(agent_id, session, storage)
    try:
        excerpt = await asyncio.to_thread(inspector.read, path, start_line, end_line)
    except SourceInspectError as error:
        status = 404 if error.code == "file-not-found" else 422
        raise HTTPException(status_code=status, detail=str(error)) from error
    logger.info(
        "admin_actor=%s read screening source agent_id=%s path=%s lines=%s-%s",
        x_admin_actor,
        agent_id,
        path,
        excerpt["start_line"],
        excerpt["end_line"],
    )
    await record_artifact_fetch(
        session,
        agent_id=agent_id,
        endpoint=ENDPOINT_ADMIN_SOURCE_FILE,
        requester_kind="admin",
        requester_id=x_admin_actor,
        artifact_sha256=agent.sha256,
        source_ip=client_ip(request),
        detail=request_detail(
            request,
            path=path,
            start_line=excerpt["start_line"],
            end_line=excerpt["end_line"],
        ),
    )
    return AdminSourceExcerpt(agent_id=agent_id, **excerpt)  # type: ignore[arg-type]


@router.get(
    "/screening-submissions/{agent_id}/source-search",
    response_model=AdminSourceSearchResult,
)
async def search_screening_source(
    agent_id: UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    pattern: Annotated[str, Query(min_length=1, max_length=MAX_SEARCH_PATTERN_CHARS)],
    mode: Literal["regex", "literal"] = "regex",
    ignore_case: bool = False,
    path_glob: Annotated[str | None, Query(max_length=240)] = None,
    context: Annotated[int, Query(ge=0, le=MAX_SEARCH_CONTEXT)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_MATCHES)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminSourceSearchResult:
    """Audited grep across one submission's readable source, in one request.

    The source-review question is almost always *where* — where the agent
    constructs its graded ``RunResponse``, where a flagged import is used,
    where a hardcoded answer table lives. The manifest cannot answer it and
    :func:`read_screening_source_file` is capped at 400 lines, so on the
    10,000-line ``baseline.rs`` a real submission ships, locating the code
    meant bisecting with six to eight blind reads. This returns
    ``path:line:text`` for the whole artifact in one call, and the operator
    then reads only the region it names.

    Read-only and bounded on every axis: opaque members are skipped and
    counted, the scan stops at its match cap, lines are clipped, and the page
    reports ``has_more``.
    """
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    agent, inspector = await _load_inspector(agent_id, session, storage)
    try:
        found = await asyncio.to_thread(
            inspector.search,
            pattern,
            mode=mode,
            ignore_case=ignore_case,
            path_glob=path_glob,
            context=context,
            limit=limit,
            offset=offset,
        )
    except SourceInspectError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    logger.info(
        "admin_actor=%s searched screening source agent_id=%s mode=%s matches=%s",
        x_admin_actor,
        agent_id,
        mode,
        found["match_count"],
    )
    await record_artifact_fetch(
        session,
        agent_id=agent_id,
        endpoint=ENDPOINT_ADMIN_SOURCE_SEARCH,
        requester_kind="admin",
        requester_id=x_admin_actor,
        artifact_sha256=agent.sha256,
        source_ip=client_ip(request),
        # The pattern is recorded: the audit trail's job is to show what an
        # operator went looking for in miner source, not merely that they did.
        detail=request_detail(
            request,
            pattern=pattern,
            mode=mode,
            path_glob=path_glob,
            match_count=found["match_count"],
        ),
    )
    return AdminSourceSearchResult(
        agent_id=agent_id,
        artifact_sha256=agent.sha256,
        **found,  # type: ignore[arg-type]
    )


async def _baseline_pair(
    agent_id: UUID, session: AsyncSession, storage: S3StorageClient
) -> tuple[Agent, dict[str, str], dict[str, str], bool]:
    """Load one submission's text map aligned against the starter-kit baseline."""
    agent, inspector = await _load_inspector(agent_id, session, storage)
    raw_text = await asyncio.to_thread(inspector.read_all_text)
    candidate = await asyncio.to_thread(align_candidate_paths, raw_text)
    return agent, candidate, starter_kit_head_text(), candidate is not raw_text


@router.get(
    "/screening-submissions/{agent_id}/baseline-diff",
    response_model=AdminBaselineDiffManifest,
)
async def get_screening_baseline_diff(
    agent_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminBaselineDiffManifest:
    """Per-file diff between one submission and the starter kit it derives from.

    Every submission descends from the official starter kit, so reviewing a
    quarantine cold means reading a whole crate to find the few files the miner
    actually wrote. This classifies each path against the pinned baseline and
    marks stock kit code — including files that match an older kit revision
    rather than the tip — so the operator can go straight to the custom surface.

    Unified-diff bodies come from the per-file endpoint.
    """
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    agent, candidate, baseline, aligned = await _baseline_pair(
        agent_id, session, storage
    )
    manifest = await asyncio.to_thread(
        build_baseline_diff_manifest, candidate, baseline, is_stock_kit_text
    )
    provenance = starter_kit_provenance()
    logger.info(
        "admin_actor=%s viewed baseline diff agent_id=%s custom_lines=%s revision=%s",
        x_admin_actor,
        agent_id,
        manifest["custom_added_lines"],
        provenance["revision"],
    )
    return AdminBaselineDiffManifest(
        agent_id=agent_id,
        artifact_sha256=agent.sha256,
        baseline=AdminStarterKitProvenance(
            source=provenance["source"],
            revision=provenance["revision"],
            commit_set_sha256=provenance["commit_set_sha256"],
            commit_count=int(provenance["commit_count"]),
        ),
        path_aligned=aligned,
        **manifest,  # type: ignore[arg-type]
    )


@router.get(
    "/screening-submissions/{agent_id}/baseline-diff/file",
    response_model=AdminBaselineDiffFileDetail,
)
async def read_screening_baseline_diff_file(
    agent_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    path: Annotated[str, Query(min_length=1, max_length=240)],
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminBaselineDiffFileDetail:
    """Bounded unified diff (starter kit -> submission) for one file."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    normalized = path.removeprefix("./")
    _agent, candidate, baseline, _aligned = await _baseline_pair(
        agent_id, session, storage
    )
    try:
        detail = await asyncio.to_thread(
            unified_diff_for_file,
            normalized,
            candidate,
            baseline,
            pair_renames=False,
        )
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail=f"no file at {normalized!r} in the submission or the baseline",
        ) from error
    text = candidate.get(normalized)
    logger.info(
        "admin_actor=%s viewed baseline file diff agent_id=%s path=%s",
        x_admin_actor,
        agent_id,
        normalized,
    )
    return AdminBaselineDiffFileDetail(
        agent_id=agent_id,
        stock_kit=text is not None and is_stock_kit_text(text),
        **detail,  # type: ignore[arg-type]
    )


__all__ = ["router"]
