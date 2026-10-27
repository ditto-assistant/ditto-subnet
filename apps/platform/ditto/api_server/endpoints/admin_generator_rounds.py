"""Audited operator control and inspection for generator-competition rounds.

Every round fact an operator needs to run or debug the matrix is readable
here: the chain schedule, each round's commitment and which elected
validators published it on-chain, frozen columns and panel, generation per
column, every validator lease and result, the agents still pending, the
scored matrix, and the round driver's last tick. Writes are
confirmation-gated and guarded by the round's current status. The round
secret is never served before the reveal; the seed and dataset are served to
operators only once the round closed (or, for the dataset, through the
artifact-scoped download used to reproduce a round).
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import MAX_SOURCE_ARCHIVE_BYTES
from ditto.api_models.generator_round_validator import GENERATOR_ROUND_BENCH_VERSIONS
from ditto.api_models.generator_rounds import (
    DEFAULT_GENERATOR_UPLOAD_CONTENT_TYPE,
    DEFAULT_GENERATOR_UPLOAD_PREFIX,
    DEFAULT_GENERATOR_UPLOAD_TTL_SECONDS,
    REGISTER_DEFAULT_GENERATOR_CONFIRMATION,
    RELEASE_ROUND_LEASE_CONFIRMATION,
    REPUBLISH_COMMITMENT_CONFIRMATION,
    AdminDefaultGeneratorRequest,
    AdminDefaultGeneratorResponse,
    AdminDefaultGeneratorUploadResponse,
    AdminGeneratorRoundActionRequest,
    AdminGeneratorRoundDetail,
    AdminGeneratorRoundLeaseReleaseRequest,
    AdminGeneratorRoundListResponse,
    AdminGeneratorRoundPreview,
    GeneratorRoundBenchContractPreview,
    GeneratorRoundColumnSlot,
    GeneratorRoundLoopStatus,
    GeneratorRoundPanelSlot,
    GeneratorRoundSchedule,
    GeneratorRoundSkippedAgent,
    void_round_confirmation,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_ath_rulings import ActorDep, actor_slug
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.generator_competition import (
    InspectedUpload,
    UploadRejected,
    inspect_source,
    inspect_upload,
    load_settings,
)
from ditto.api_server.generator_rounds import (
    ROUND_COMMITMENT_PUBLISHERS,
    GeneratorRoundLoop,
    attempt_views,
    choose_round_bench_version,
    close_round,
    column_views,
    commitment_publication_views,
    expire_round_tickets,
    get_round,
    list_rounds,
    pending_panel_agents,
    public_default_generator,
    result_views,
    round_contract_capacity,
    round_counts_by_status,
    round_summaries,
    round_summary,
    select_columns,
    select_round_panel,
    standing_views,
    void_round,
)
from ditto.api_server.hippius import HippiusClient, normalize_object_key
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
)
from ditto.db.models import (
    GeneratorRoundAttempt,
    GeneratorRoundCommitmentPublication,
    GeneratorSubmission,
    ValidatorTicket,
)
from ditto.db.queries.audit import append_audit_entry
from ditto.db.queries.generator import (
    get_submission_by_sha,
    get_submission_by_tree,
    lock_source_tree,
    record_operator_action,
)
from ditto.db.queries.inference import revoke_ticket_inference
from ditto.db.queries.tickets import finish_generator_round_ticket

router = APIRouter(prefix="/admin/generator-competition", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
MIN_REASON_LENGTH = 8


class GeneratorRoundLoopResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    available: bool
    """``False`` when this process does not run the round driver."""
    status: GeneratorRoundLoopStatus | None
    schedule: GeneratorRoundSchedule | None


class GeneratorRoundTickResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    actions: list[str]
    schedule: GeneratorRoundSchedule | None
    error: str | None = None


class GeneratorRoundDatasetResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    round_index: int
    dataset_sha256: str
    seed: str
    size_bytes: int
    dataset_base64: str


def _loop(request: Request) -> GeneratorRoundLoop | None:
    return getattr(request.app.state, "generator_round_loop", None)


def _schedule(request: Request, mode: str, per: int) -> GeneratorRoundSchedule:
    loop = _loop(request)
    if loop is not None and loop.schedule is not None:
        return loop.schedule
    return GeneratorRoundSchedule(mode=mode, epochs_per_round=per)


def _check_reason(reason: str, actor: str) -> tuple[str, str]:
    reason, actor = reason.strip(), actor.strip()
    if len(reason) < MIN_REASON_LENGTH or not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="reason and actor are required")
    return reason, actor


@router.get("/rounds", response_model=AdminGeneratorRoundListResponse)
async def admin_list_rounds(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminGeneratorRoundListResponse:
    """Recent rounds newest first, the chain schedule, and counts by status."""
    resolved = await load_settings(session)
    rows = await list_rounds(session, limit=limit, offset=offset)
    return AdminGeneratorRoundListResponse(
        schedule=_schedule(
            request, resolved.settings.mode, resolved.settings.epochs_per_round
        ),
        rounds=await round_summaries(session, rows, operator=True),
        counts_by_status=await round_counts_by_status(session),
    )


@router.get("/rounds/loop", response_model=GeneratorRoundLoopResponse)
async def admin_round_loop(
    request: Request, _admin: AdminDep
) -> GeneratorRoundLoopResponse:
    """The round driver's last tick: actions taken, errors, chain position."""
    loop = _loop(request)
    if loop is None:
        return GeneratorRoundLoopResponse(available=False, status=None, schedule=None)
    return GeneratorRoundLoopResponse(
        available=True, status=loop.status, schedule=loop.schedule
    )


@router.post("/rounds/tick", response_model=GeneratorRoundTickResponse)
async def admin_round_tick(
    request: Request, _admin: AdminDep
) -> GeneratorRoundTickResponse:
    """Run one round-driver tick now instead of waiting for the next one.

    Ticks are idempotent: at a given chain position a second tick changes
    nothing, so this only ever applies transitions that are already due.
    """
    loop = _loop(request)
    if loop is None:
        loop = GeneratorRoundLoop(
            app_state=request.app.state, session_maker=request.app.state.session_maker
        )
    try:
        actions = await loop.tick()
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return GeneratorRoundTickResponse(
            actions=[],
            schedule=loop.schedule,
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    return GeneratorRoundTickResponse(actions=actions.actions, schedule=loop.schedule)


@router.get("/rounds/preview", response_model=AdminGeneratorRoundPreview)
async def admin_round_preview(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminGeneratorRoundPreview:
    """What a round opened now would freeze, and every reason it could not."""
    resolved = await load_settings(session)
    settings = resolved.settings
    columns = await select_columns(session, settings)
    frozen = await select_round_panel(session, settings)
    panel, skipped = frozen.panel, frozen.skipped_references
    qualified = int(
        await session.scalar(
            select(func.count()).where(
                GeneratorSubmission.status == "active",
                GeneratorSubmission.qualified_at.is_not(None),
                GeneratorSubmission.public_default.is_(False),
            )
        )
        or 0
    )
    awaiting = int(
        await session.scalar(
            select(func.count()).where(
                GeneratorSubmission.status == "active",
                GeneratorSubmission.qualified_at.is_(None),
            )
        )
        or 0
    )
    blockers: list[str] = list(skipped)
    if settings.mode == "off":
        blockers.append("settings mode is off")
    if not resolved.valid:
        blockers.append("the governing settings revision is invalid")
    if not any(column["counted"] for column in columns):
        default = await public_default_generator(session)
        blockers.append(
            "no counted columns: no qualified generator and "
            + (
                "the public default generator is not qualified yet"
                if default is not None
                else "no public default generator is registered"
            )
        )
    if len(panel) < settings.min_panel:
        blockers.append(
            f"panel has {len(panel)} agent(s), min_panel {settings.min_panel}"
        )
    if not settings.validator_rounds_enabled:
        blockers.append(
            "validator_rounds_enabled is false: rounds generate but no validator "
            "runs the panel"
        )
    now = datetime.now(UTC)
    contract = await choose_round_bench_version(
        session, cases_per_column=settings.cases_per_seed, now=now
    )
    if contract.bench_version is None:
        blockers.append(f"no generator-round contract can run: {contract.reason}")
    capable = await round_contract_capacity(
        session, cases_per_column=settings.cases_per_seed, now=now
    )
    return AdminGeneratorRoundPreview(
        schedule=_schedule(request, settings.mode, settings.epochs_per_round),
        columns=[GeneratorRoundColumnSlot(**c) for c in columns],
        panel=[GeneratorRoundPanelSlot(**p) for p in panel],
        qualified_generators=qualified,
        awaiting_qualification=awaiting,
        blockers=blockers,
        skipped_panel_agents=[
            GeneratorRoundSkippedAgent(**slot) for slot in frozen.skipped_agents
        ],
        bench_contract=GeneratorRoundBenchContractPreview(
            active_version=contract.active_version,
            bench_version=contract.bench_version,
            reason=contract.reason,
            round_contracts=list(GENERATOR_ROUND_BENCH_VERSIONS),
            capable_validators=capable,
        ),
    )


@router.get("/rounds/{round_index}", response_model=AdminGeneratorRoundDetail)
async def admin_round_detail(
    round_index: int, _admin: AdminDep, session: SessionDep
) -> AdminGeneratorRoundDetail:
    row = await get_round(session, round_index)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown round")
    closed = row.status in {"scoring", "scored", "void"}
    now = datetime.now(UTC)
    publications, elected = await commitment_publication_views(session, row, now=now)
    return AdminGeneratorRoundDetail(
        summary=await round_summary(session, row, operator=True),
        settings_revision=row.settings_revision,
        cases_per_column=row.cases_per_column,
        bench_version=row.bench_version,
        seed=row.seed if closed else None,
        columns=[GeneratorRoundColumnSlot(**c) for c in row.columns or ()],
        panel=[GeneratorRoundPanelSlot(**p) for p in row.panel or ()],
        generation=row.generation,
        column_case_counts={k: len(v) for k, v in (row.column_cases or {}).items()},
        dataset_bytes=len(row.dataset_bytes) if row.dataset_bytes else None,
        lease_id=row.lease_id,
        lease_expires_at=row.lease_expires_at,
        lease_attempts=row.lease_attempts,
        commitment_published_at=row.commitment_published_at,
        commitment_error=row.commitment_error,
        commitment_publishers_target=ROUND_COMMITMENT_PUBLISHERS,
        commitment_publications=publications,
        commitment_elected=elected,
        attempts=await attempt_views(session, round_index),
        results=await result_views(session, row),
        pending_agents=(
            await pending_panel_agents(session, row, now=now)
            if row.status == "running"
            else []
        ),
        skipped_panel_agents=[
            GeneratorRoundSkippedAgent(**slot)
            for slot in (row.generation or {}).get("skipped_panel_agents", ())
        ],
        score=row.score,
        scorer_version=row.scorer_version,
        standings=standing_views(row),
        columns_scored=column_views(row),
    )


@router.get(
    "/rounds/{round_index}/dataset", response_model=GeneratorRoundDatasetResponse
)
async def admin_round_dataset(
    round_index: int, _admin: AdminDep, session: SessionDep
) -> GeneratorRoundDatasetResponse:
    """The exact round dataset bytes, for reproducing a closed round."""
    row = await get_round(session, round_index)
    if row is None or row.dataset_bytes is None or row.dataset_sha256 is None:
        raise HTTPException(status_code=404, detail="round has no dataset")
    if row.status not in {"scoring", "scored", "void"}:
        raise HTTPException(
            status_code=409, detail="the dataset is private while the round runs"
        )
    assert row.seed is not None
    return GeneratorRoundDatasetResponse(
        round_index=row.round_index,
        dataset_sha256=row.dataset_sha256,
        seed=row.seed,
        size_bytes=len(row.dataset_bytes),
        dataset_base64=base64.b64encode(row.dataset_bytes).decode("ascii"),
    )


async def _locked_round(
    session: AsyncSession, round_index: int, payload: AdminGeneratorRoundActionRequest
):
    reason, actor = _check_reason(payload.reason, payload.actor)
    row = await get_round(session, round_index, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown round")
    if row.status != payload.expected_status:
        raise HTTPException(
            status_code=409,
            detail=f"round is {row.status}, not {payload.expected_status}; re-read it",
        )
    return row, reason, actor


@router.post("/rounds/{round_index}/void", response_model=AdminGeneratorRoundDetail)
async def admin_void_round(
    round_index: int,
    payload: AdminGeneratorRoundActionRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminGeneratorRoundDetail:
    """Void a round by operator ruling. Live round leases are expired and no
    column score from it is ever credited."""
    if payload.confirmation != void_round_confirmation(round_index):
        raise HTTPException(
            status_code=409,
            detail=(
                f"confirmation must be exactly {void_round_confirmation(round_index)}"
            ),
        )
    row, reason, actor = await _locked_round(session, round_index, payload)
    if row.status in {"scored", "void"}:
        raise HTTPException(status_code=409, detail=f"a {row.status} round is final")
    now = datetime.now(UTC)
    await expire_round_tickets(session, row, now=now, detail=f"operator void: {reason}")
    void_round(row, f"operator ({actor}): {reason}", now=now)
    record_operator_action(
        session,
        action="void",
        subject=f"round:{round_index}",
        actor=actor,
        reason=reason,
    )
    await session.commit()
    return await admin_round_detail(round_index, _admin, session)


@router.post("/rounds/{round_index}/close", response_model=AdminGeneratorRoundDetail)
async def admin_close_round(
    round_index: int,
    payload: AdminGeneratorRoundActionRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminGeneratorRoundDetail:
    """Close a running round now and score it with the results it has.

    For a round that will not complete (a panel agent no validator can run).
    Fewer than ``min_panel`` results voids it instead.
    """
    if payload.confirmation != f"CLOSE GENERATOR ROUND {round_index}":
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly CLOSE GENERATOR ROUND {round_index}",
        )
    row, reason, actor = await _locked_round(session, round_index, payload)
    if row.status != "running":
        raise HTTPException(status_code=409, detail="only a running round closes")
    # close_round judges min_panel by the round's own settings revision.
    outcome = await close_round(
        session,
        row,
        now=datetime.now(UTC),
        reason=f"operator ({actor}): {reason}",
    )
    record_operator_action(
        session,
        action="close",
        subject=f"round:{round_index}",
        actor=actor,
        reason=reason,
        detail={"outcome": outcome},
    )
    await session.commit()
    return await admin_round_detail(round_index, _admin, session)


@router.post(
    "/rounds/{round_index}/republish-commitment",
    response_model=AdminGeneratorRoundDetail,
)
async def admin_republish_commitment(
    round_index: int,
    payload: AdminGeneratorRoundActionRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminGeneratorRoundDetail:
    """Ask elected validators to publish a committed round's commitment again.

    Platform never publishes a commitment itself. This reopens publication
    (``pending``) and forgets every failed or unconfirmed validator claim, so
    the round's publisher order elects again from the validators fresh now.
    Confirmed publications are kept and still count toward the target.
    """
    if payload.confirmation != REPUBLISH_COMMITMENT_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {REPUBLISH_COMMITMENT_CONFIRMATION}",
        )
    row, reason, actor = await _locked_round(session, round_index, payload)
    if row.status != "committed":
        raise HTTPException(
            status_code=409,
            detail="a commitment published after the round opened proves nothing",
        )
    if row.commitment_publication == "published":
        raise HTTPException(
            status_code=409,
            detail="commitment publication already reached its validator target",
        )
    cleared = await session.execute(
        delete(GeneratorRoundCommitmentPublication).where(
            GeneratorRoundCommitmentPublication.round_index == round_index,
            GeneratorRoundCommitmentPublication.status != "confirmed",
        )
    )
    record_operator_action(
        session,
        action="republish_commitment",
        subject=f"round:{round_index}",
        actor=actor,
        reason=reason,
        detail={
            "previous": row.commitment_publication,
            "cleared_claims": int(cleared.rowcount or 0),  # type: ignore[attr-defined]
        },
    )
    row.commitment_publication = "pending"
    row.commitment_error = None
    await session.commit()
    session.expire_all()
    return await admin_round_detail(round_index, _admin, session)


@router.post(
    "/rounds/{round_index}/release-lease", response_model=AdminGeneratorRoundDetail
)
async def admin_release_round_lease(
    round_index: int,
    payload: AdminGeneratorRoundLeaseReleaseRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminGeneratorRoundDetail:
    """Release one validator's live round lease so the agent can be re-leased.

    For a validator that took a panel run and went quiet: the lease would
    otherwise hold the agent until it expires.
    """
    if payload.confirmation != RELEASE_ROUND_LEASE_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {RELEASE_ROUND_LEASE_CONFIRMATION}",
        )
    reason, actor = _check_reason(payload.reason, payload.actor)
    row = await get_round(session, round_index)
    if row is None or row.dataset_sha256 is None:
        raise HTTPException(status_code=404, detail="round has no dataset")
    now = datetime.now(UTC)
    # The round's own leased attempt names the exact ticket (by deadline), so a
    # ticket another round issued on an identical dataset is never touched.
    attempt = await session.scalar(
        select(GeneratorRoundAttempt)
        .where(
            GeneratorRoundAttempt.round_index == round_index,
            GeneratorRoundAttempt.agent_id == payload.agent_id,
            GeneratorRoundAttempt.validator_hotkey == payload.validator_hotkey,
            GeneratorRoundAttempt.status == "leased",
        )
        .with_for_update()
    )
    ticket = (
        await session.scalar(
            select(ValidatorTicket)
            .where(
                ValidatorTicket.agent_id == payload.agent_id,
                ValidatorTicket.validator_hotkey == payload.validator_hotkey,
                ValidatorTicket.purpose == TicketPurpose.GENERATOR_ROUND,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.dataset_sha256 == row.dataset_sha256,
                ValidatorTicket.deadline == attempt.ticket_deadline,
            )
            .with_for_update()
        )
        if attempt is not None
        else None
    )
    if attempt is None or ticket is None:
        raise HTTPException(status_code=409, detail="no live round lease to release")
    await revoke_ticket_inference(session, ticket=ticket, now=now)
    finish_generator_round_ticket(ticket)
    attempt.status = "failed"
    attempt.detail = f"operator ({actor}): {reason}"
    attempt.finished_at = now
    record_operator_action(
        session,
        action="release_lease",
        subject=f"round:{round_index}",
        actor=actor,
        reason=reason,
        detail={
            "agent_id": str(payload.agent_id),
            "validator_hotkey": payload.validator_hotkey,
        },
    )
    await append_audit_entry(
        session,
        agent_id=payload.agent_id,
        validator_hotkey=payload.validator_hotkey,
        event="generator_round_lease_released",
        payload={"round_index": round_index, "actor": actor, "reason": reason},
        recorded_at=now,
    )
    await session.commit()
    return await admin_round_detail(round_index, _admin, session)


def _default_generator_store(request: Request) -> HippiusClient:
    # The private inference-trace bucket, as ATH rulings uploads use; never a
    # bucket that may serve a public prefix.
    client = getattr(request.app.state, "traces_hippius", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "default generator upload storage is not configured; send source_base64"
            ),
        )
    return client


def _default_generator_prefix(actor: str) -> str:
    # The slug is readable but lossy (case and punctuation fold), so a digest
    # of the exact actor keeps two operators with one slug in distinct scopes.
    digest = hashlib.sha256(actor.encode()).hexdigest()[:16]
    return f"{DEFAULT_GENERATOR_UPLOAD_PREFIX}{actor_slug(actor)}-{digest}/"


def _header_actor(x_admin_actor: str | None) -> str:
    actor = (x_admin_actor or "").strip()
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return actor


@router.post(
    "/default-generator/upload-url",
    response_model=AdminDefaultGeneratorUploadResponse,
)
async def admin_create_default_generator_upload(
    request: Request, _admin: AdminDep, x_admin_actor: ActorDep = None
) -> AdminDefaultGeneratorUploadResponse:
    """Issue a five-minute presigned PUT for one default-generator archive.

    Register it with ``POST /default-generator`` and ``upload_key``; the key is
    scoped to the issuing operator.
    """
    actor = _header_actor(x_admin_actor)
    client = _default_generator_store(request)
    now = datetime.now(UTC)
    key = f"{_default_generator_prefix(actor)}{now:%Y-%m-%d}/{uuid4()}.tar.gz"
    url = await client.presigned_put_url(
        key=key,
        content_type=DEFAULT_GENERATOR_UPLOAD_CONTENT_TYPE,
        expires_in=DEFAULT_GENERATOR_UPLOAD_TTL_SECONDS,
    )
    return AdminDefaultGeneratorUploadResponse(
        bucket=client.bucket,
        key=key,
        url=url,
        content_type=DEFAULT_GENERATOR_UPLOAD_CONTENT_TYPE,
        expires_in=DEFAULT_GENERATOR_UPLOAD_TTL_SECONDS,
        max_bytes=MAX_SOURCE_ARCHIVE_BYTES,
    )


async def _inspect_default_source(
    request: Request,
    payload: AdminDefaultGeneratorRequest,
    x_admin_actor: str | None,
) -> InspectedUpload:
    if payload.upload_key is None:
        assert payload.source_base64 is not None
        return inspect_upload(
            source_base64=payload.source_base64, source_sha256=payload.source_sha256
        )
    key = normalize_object_key(payload.upload_key)
    # Scope by the authenticated actor header, as the issuing route does, never
    # by the free-form body actor.
    scope = _default_generator_prefix(_header_actor(x_admin_actor))
    if not key.startswith(scope):
        raise HTTPException(
            status_code=403,
            detail=f"upload_key must be under this operator's prefix {scope}",
        )
    client = _default_generator_store(request)
    try:
        raw = await client.get_object(key=key, max_bytes=MAX_SOURCE_ARCHIVE_BYTES)
    except ObjectNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="default generator upload not found; upload it first",
        ) from None
    except ObjectDownloadFailedError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "default generator upload could not be read within "
                f"{MAX_SOURCE_ARCHIVE_BYTES} bytes: {exc}"
            ),
        ) from None
    return inspect_source(raw, source_sha256=payload.source_sha256)


@router.post("/default-generator", response_model=AdminDefaultGeneratorResponse)
async def admin_register_default_generator(
    payload: AdminDefaultGeneratorRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: ActorDep = None,
) -> AdminDefaultGeneratorResponse:
    """Register the public default generator that fills empty counted columns.

    The source is checked like any upload, owned by nobody, never on the
    ledger, and qualified by the scorer like any generator before it can fill
    a column. A new registration supersedes the previous default.
    """
    if payload.confirmation != REGISTER_DEFAULT_GENERATOR_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=(
                "confirmation must be exactly "
                f"{REGISTER_DEFAULT_GENERATOR_CONFIRMATION}"
            ),
        )
    reason, actor = _check_reason(payload.reason, payload.actor)
    try:
        inspected = await _inspect_default_source(request, payload, x_admin_actor)
    except UploadRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    info = inspected.info
    # The same tree lock miner uploads take, so a concurrent upload of these
    # files cannot also register them.
    await lock_source_tree(session, info.tree_sha256)
    existing = await get_submission_by_sha(session, payload.source_sha256)
    if existing is None:
        existing = await get_submission_by_tree(session, info.tree_sha256)
    if existing is not None and not existing.public_default:
        raise HTTPException(
            status_code=409,
            detail=f"this source is already miner submission {existing.submission_id}",
        )
    if existing is not None and existing.status == "active":
        return AdminDefaultGeneratorResponse(
            submission_id=existing.submission_id,
            source_sha256=existing.source_sha256,
            tree_sha256=existing.tree_sha256,
            status="active",
        )
    now = datetime.now(UTC)
    superseded = list(
        await session.scalars(
            update(GeneratorSubmission)
            .where(
                GeneratorSubmission.public_default.is_(True),
                GeneratorSubmission.status == "active",
            )
            .values(
                status="superseded",
                status_reason=f"default generator replaced by operator ({actor})",
                updated_at=now,
            )
            .returning(GeneratorSubmission.submission_id)
        )
    )
    row = GeneratorSubmission(
        submission_id=uuid4(),
        miner_hotkey=payload.miner_hotkey,
        owner_key="public-default",
        source_sha256=payload.source_sha256,
        tree_sha256=info.tree_sha256,
        source_bytes=inspected.raw,
        size_bytes=len(inspected.raw),
        module=info.module,
        go_version=info.go_version,
        file_count=info.files,
        status="active",
        status_reason=f"public default generator: {reason}"[:1000],
        attempts=0,
        next_run_at=now,
        submitted_at=now,
        updated_at=now,
        public_default=True,
    )
    session.add(row)
    record_operator_action(
        session,
        action="register_default_generator",
        subject=str(row.submission_id),
        actor=actor,
        reason=reason,
        detail={
            "source_sha256": payload.source_sha256,
            "upload_key": payload.upload_key,
            "superseded": [str(sid) for sid in superseded],
        },
    )
    await session.commit()
    assert hashlib.sha256(inspected.raw).hexdigest() == payload.source_sha256
    return AdminDefaultGeneratorResponse(
        submission_id=row.submission_id,
        source_sha256=row.source_sha256,
        tree_sha256=row.tree_sha256,
        status="active",
        superseded=superseded,
    )
