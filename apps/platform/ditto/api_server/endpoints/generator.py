"""Generator competition: miner uploads and the public board (shadow).

``POST /generator/submissions`` accepts a signed generator source archive. The
checks run cheap-before-expensive and every mutation happens after every check
passes: lane open, fresh domain-separated signature, ban, the fee proof's
presence and replay status (only while the generator fee is required), subnet
registration, archive bytes and structure (the Go contract's rules), duplicate
source, duplicate tree, cooldown, the on-chain fee verification, then one
insert that supersedes the hotkey's previous generator and records the fee.

``GET /generator/upload-terms`` tells a client, before it pays anything,
whether a fee is required, its amount and receiving address, and every
refusal Platform can already decide without the archive.

The fee is the generator competition's own: the submission settings'
``competition_fees.generator`` override, whose amount inherits the default
(agent-upload) fee while unset and whose ``fee_required`` inherits the
generator policy's ``upload_fee_required`` while unset. A proof must pay
exactly that amount, so a transfer sized for the agent fee cannot fund a
differently priced generator upload (and one proof never funds two uploads
in either lane).

Source never leaves Platform except to the trusted scorer. The listing and
board here expose identities, lifecycle, and aggregate outcomes only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, cast
from uuid import UUID

import bittensor
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import (
    GeneratorSubmissionListResponse,
    GeneratorSubmissionSummary,
    GeneratorSubmissionUploadRequest,
    GeneratorSubmissionUploadResponse,
    GeneratorUploadTerms,
    SubmissionStatus,
    normalize_generator_name,
    source_signature_payload,
)
from ditto.api_models.generator_rounds import (
    GeneratorRoundDetail,
    GeneratorRoundListResponse,
    GeneratorRoundSchedule,
    PublicGeneratorBoardEntry,
    PublicGeneratorBoardResponse,
    PublicGeneratorSubmissionDetail,
)
from ditto.api_server.dependencies import (
    get_chain_client,
    get_payment_verifier,
    get_session,
)
from ditto.api_server.generator_competition import (
    UploadRejected,
    build_ledger,
    inspect_upload,
    load_settings,
    submission_summary,
)
from ditto.api_server.generator_identity import (
    agent_identities,
    generator_identities,
)
from ditto.api_server.generator_rounds import (
    column_views,
    get_round,
    hotkey_round_points,
    list_rounds,
    round_summaries,
    round_summary,
    standing_views,
    submission_round_views,
)
from ditto.api_server.payment_verifier import (
    PaymentProof,
    PaymentRecoveryExpired,
    PaymentReplayedError,
    PaymentVerifier,
    VerifiedPayment,
)
from ditto.chain.errors import ChainError
from ditto.db.queries.attestation import get_bound_coldkey_for_hotkey
from ditto.db.queries.bans import is_hotkey_banned
from ditto.db.queries.generator import (
    case_status_counts,
    get_submission,
    get_submission_by_sha,
    get_submission_by_tree,
    get_submission_without_source,
    insert_submission,
    latest_runs,
    latest_submitted_at,
    list_submissions,
    lock_hotkey_uploads,
    lock_source_tree,
    supersede_active,
    superseded_by,
)
from ditto.db.queries.name_claims import upload_name_is_reserved
from ditto.db.queries.payments import (
    get_evaluation_payment_for_proof,
    get_generator_payment_for_proof,
    insert_generator_payment,
)
from ditto.db.queries.scores import emission_owner
from ditto.db.queries.submission_settings import (
    UPLOAD_ADMISSION_TTL,
    CompetitionFee,
    effective_submission_settings,
)

if TYPE_CHECKING:
    from ditto.chain.client import ChainClient
    from ditto.db.models import GeneratorSubmission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generator", tags=["generator"])

SIGNATURE_MAX_SKEW_SECONDS = 300
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
# The board and rounds are the same for every viewer. The public cache serves
# them for 30s and then answers from the last snapshot while one background
# read rebuilds it, so a dashboard poll never waits on the ledger query.
_PUBLIC_READ_CACHE = "public, max-age=30, stale-while-revalidate=120"
# Each board entry carries the hotkey's latest scored columns for a sparkline.
PUBLIC_BOARD_RECENT_ROUNDS = 24

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated["ChainClient", Depends(get_chain_client)]

FEE_REQUIRED_DETAIL = (
    "generator uploads require a TAO fee: send payment_block_hash, "
    "payment_block_number and payment_extrinsic_index for a finalized transfer "
    "of the fee in GET /generator/upload-terms. Upgrade the ditto CLI and rerun "
    "`ditto generator upload`; it pays the fee and attaches the proof"
)


def _verify_signature(payload: GeneratorSubmissionUploadRequest) -> bool:
    if (
        payload.signature_nonce.version != 4
        or abs(time.time() - payload.signature_timestamp) > SIGNATURE_MAX_SKEW_SECONDS
    ):
        return False
    message = source_signature_payload(
        hotkey=payload.hotkey,
        source_sha256=payload.source_sha256,
        timestamp=payload.signature_timestamp,
        nonce=payload.signature_nonce,
    )
    signature = payload.signature.removeprefix("0x")
    try:
        keypair = bittensor.Keypair(ss58_address=payload.hotkey)
        return bool(keypair.verify(message, bytes.fromhex(signature)))
    except (ValueError, TypeError):
        return False


def _ensure_payment_fresh(verified: VerifiedPayment) -> None:
    """A fee older than the agent-upload recovery window cannot fund an upload."""
    paid_at = verified.block_timestamp
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=UTC)
    if paid_at + UPLOAD_ADMISSION_TTL <= datetime.now(UTC):
        raise PaymentRecoveryExpired(
            f"payment at {paid_at.isoformat()} is outside the 24-hour window"
        )


async def _replayed_or_retry(
    session: AsyncSession, payload: GeneratorSubmissionUploadRequest
) -> GeneratorSubmissionUploadResponse | None:
    """Refuse a proof another upload already spent; recover an exact retry.

    A gateway failure can hide the 200 after the commit. The same hotkey
    re-sending the same archive with the same proof gets its submission back
    instead of a refusal, so the miner never pays twice.
    """
    assert payload.payment_block_hash is not None
    assert payload.payment_extrinsic_index is not None
    funded = await get_generator_payment_for_proof(
        session,
        block_hash=payload.payment_block_hash,
        extrinsic_index=payload.payment_extrinsic_index,
    )
    if funded is not None:
        row = await get_submission(session, funded.submission_id)
        if (
            row is not None
            and row.miner_hotkey == payload.hotkey
            and row.source_sha256 == payload.source_sha256
        ):
            return GeneratorSubmissionUploadResponse(
                submission_id=row.submission_id,
                source_sha256=row.source_sha256,
                tree_sha256=row.tree_sha256,
                status=cast("SubmissionStatus", row.status),
                superseded=await superseded_by(
                    session, miner_hotkey=row.miner_hotkey, keep=row.submission_id
                ),
                name=row.name,
            )
        raise PaymentReplayedError("payment proof already funded another upload")
    if await get_evaluation_payment_for_proof(
        session,
        block_hash=payload.payment_block_hash,
        extrinsic_index=payload.payment_extrinsic_index,
    ):
        raise PaymentReplayedError("payment proof already funded an agent upload")
    return None


GENERATOR_COMPETITION = "generator"


async def _generator_fee(
    session: AsyncSession, request: Request, *, upload_fee_required: bool
) -> tuple[CompetitionFee, str]:
    """The generator competition's resolved fee and the receiving address."""
    effective = await effective_submission_settings(
        session,
        default_payment_address=request.app.state.config.upload_payment_address,
    )
    return (
        effective.competition_fee(
            GENERATOR_COMPETITION, competition_requires_fee=upload_fee_required
        ),
        effective.payment_address,
    )


@router.get("/upload-terms", response_model=GeneratorUploadTerms)
async def upload_terms(
    request: Request,
    session: SessionDep,
    response: Response,
    chain: ChainDep,
    hotkey: Annotated[str | None, Query(pattern=_SS58)] = None,
    source_sha256: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
    name: Annotated[str | None, Query(max_length=256)] = None,
) -> GeneratorUploadTerms:
    """What an upload needs now; read it before paying any fee."""
    response.headers["Cache-Control"] = "no-store"
    settings = (await load_settings(session)).settings
    accepting = settings.mode != "off"
    blockers: list[str] = []
    if not accepting:
        blockers.append("the generator competition is not accepting submissions")
    payment_required = False
    amount_rao: int | None = None
    send_address: str | None = None
    amount_source = None
    if accepting:
        # A closed lane charges nothing, so it never resolves the fee.
        fee, address = await _generator_fee(
            session, request, upload_fee_required=settings.upload_fee_required
        )
        payment_required = fee.fee_required
        if payment_required:
            amount_rao, send_address = fee.fee_amount_rao, address
            amount_source = fee.amount_source
    retry_at = None
    if hotkey is not None:
        if await is_hotkey_banned(session, hotkey=hotkey):
            blockers.append("hotkey is banned from submitting")
        last = await latest_submitted_at(session, miner_hotkey=hotkey)
        if last is not None:
            candidate = last + timedelta(seconds=settings.upload_cooldown_seconds)
            if candidate > datetime.now(UTC):
                retry_at = candidate
                blockers.append(
                    f"generator upload cooldown until {retry_at.isoformat()}"
                )
    if name is not None:
        blocker = await _name_blocker(
            session,
            request,
            name=name,
            hotkey=hotkey,
            coldkey=await _registered_coldkey(request, chain, hotkey),
        )
        if blocker is not None:
            blockers.append(blocker)
    if source_sha256 is not None:
        existing = await get_submission_by_sha(session, source_sha256)
        if existing is not None:
            blockers.append(
                f"this exact archive was already uploaded as {existing.submission_id}"
            )
    return GeneratorUploadTerms(
        accepting=accepting,
        payment_required=payment_required,
        payment_amount_rao=amount_rao,
        payment_send_address=send_address,
        payment_amount_source=amount_source,
        upload_cooldown_seconds=settings.upload_cooldown_seconds,
        retry_at=retry_at,
        blockers=blockers,
    )


async def _registered_coldkey(
    request: Request, chain: ChainClient, hotkey: str | None
) -> str | None:
    """The coldkey that owns ``hotkey`` on chain, as the upload checks it.

    ``None`` when there is no hotkey, it is unregistered, or the chain read
    fails; ``_name_blocker`` then falls back to the hotkey's payment binding.
    """
    if hotkey is None:
        return None
    try:
        neurons = await chain.get_recent_neurons(request.app.state.config.chain.netuid)
    except ChainError:
        logger.warning("chain unreachable for generator upload terms", exc_info=True)
        return None
    neuron = next((n for n in neurons if n.hotkey == hotkey), None)
    return (neuron.coldkey or None) if neuron is not None else None


async def _name_blocker(
    session: AsyncSession,
    request: Request,
    *,
    name: str,
    hotkey: str | None,
    coldkey: str | None,
) -> str | None:
    """Why ``name`` cannot name this hotkey's generator, or ``None``.

    The rules are the agent-name rules: 1-64 characters (here also no control
    or invisible characters), and never a handle another miner family holds
    a signed, endorsed reservation on. Without a hotkey only the shape is
    checked. ``coldkey`` is the coldkey that owns the hotkey on chain, the
    same identity in upload terms and in the upload; only when the chain
    read fails does it fall back to the coldkey the hotkey last paid from.
    """
    try:
        normalized = normalize_generator_name(name)
    except ValueError as exc:
        return str(exc)
    if hotkey is None:
        return None
    if coldkey is None:
        coldkey = await get_bound_coldkey_for_hotkey(session, hotkey=hotkey)
    return await upload_name_is_reserved(
        session,
        netuid=request.app.state.config.chain.netuid,
        agent_name=normalized,
        miner_hotkey=hotkey,
        miner_coldkey=coldkey,
    )


@router.post("/submissions", response_model=GeneratorSubmissionUploadResponse)
async def upload_submission(
    payload: GeneratorSubmissionUploadRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> GeneratorSubmissionUploadResponse:
    """Accept one signed generator source archive and make it the hotkey's
    active generator."""
    resolved = await load_settings(session)
    settings = resolved.settings
    if settings.mode == "off":
        raise HTTPException(
            status_code=403,
            detail="the generator competition is not accepting submissions",
        )
    if not _verify_signature(payload):
        raise HTTPException(
            status_code=400, detail="signature did not verify against the hotkey"
        )
    if await is_hotkey_banned(session, hotkey=payload.hotkey):
        raise HTTPException(status_code=403, detail="hotkey is banned from submitting")
    fee, fee_address = await _generator_fee(
        session, request, upload_fee_required=settings.upload_fee_required
    )
    fee_required = fee.fee_required
    if fee_required:
        if not payload.has_payment:
            raise HTTPException(status_code=402, detail=FEE_REQUIRED_DETAIL)
        retried = await _replayed_or_retry(session, payload)
        if retried is not None:
            return retried
    if session.in_transaction():
        await session.rollback()

    netuid = request.app.state.config.chain.netuid
    try:
        neurons = await chain.get_recent_neurons(netuid)
    except ChainError as exc:
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from exc
    neuron = next((n for n in neurons if n.hotkey == payload.hotkey), None)
    if neuron is None:
        raise HTTPException(
            status_code=400, detail=f"hotkey not registered on netuid {netuid}"
        )

    try:
        upload = inspect_upload(
            source_base64=payload.source_base64, source_sha256=payload.source_sha256
        )
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.name is not None:
        refused = await _name_blocker(
            session,
            request,
            name=payload.name,
            hotkey=payload.hotkey,
            coldkey=neuron.coldkey or None,
        )
        if refused is not None:
            raise HTTPException(status_code=409, detail=refused)

    verified: VerifiedPayment | None = None
    if fee_required:
        # Refuse a doomed upload before the chain reads, without locks (the
        # locked checks below repeat these), then verify with no transaction
        # held open across the slow calls. A refused upload never spends the
        # proof, so the miner can reuse it within the 24-hour window.
        _check_duplicates_and_cooldown(
            response,
            existing=await get_submission_by_sha(session, payload.source_sha256),
            same_tree=await get_submission_by_tree(session, upload.info.tree_sha256),
            last=await latest_submitted_at(session, miner_hotkey=payload.hotkey),
            cooldown_seconds=settings.upload_cooldown_seconds,
            now=datetime.now(UTC),
        )
        if session.in_transaction():
            await session.rollback()
        assert payload.payment_block_hash is not None
        assert payload.payment_block_number is not None
        assert payload.payment_extrinsic_index is not None
        # Resolved only here: with the fee off the upload never touches it.
        verifier: PaymentVerifier = await get_payment_verifier(request)
        try:
            verified = await verifier.verify_payment(
                PaymentProof(
                    block_hash=payload.payment_block_hash,
                    block_number=payload.payment_block_number,
                    extrinsic_index=payload.payment_extrinsic_index,
                ),
                expected_hotkey=payload.hotkey,
                expected_amount_rao=fee.fee_amount_rao,
                expected_send_address=fee_address,
            )
        except ChainError as exc:
            logger.warning("chain unreachable during generator fee verify: %s", exc)
            raise HTTPException(
                status_code=503, detail="chain unavailable; retry shortly"
            ) from exc
        _ensure_payment_fresh(verified)

    await lock_hotkey_uploads(session, payload.hotkey)
    await lock_source_tree(session, upload.info.tree_sha256)
    now = datetime.now(UTC)
    _check_duplicates_and_cooldown(
        response,
        existing=await get_submission_by_sha(session, payload.source_sha256),
        same_tree=await get_submission_by_tree(session, upload.info.tree_sha256),
        last=await latest_submitted_at(session, miner_hotkey=payload.hotkey),
        cooldown_seconds=settings.upload_cooldown_seconds,
        now=now,
    )

    try:
        row = await insert_submission(
            session,
            miner_hotkey=payload.hotkey,
            owner_key=emission_owner(
                miner_hotkey=payload.hotkey, miner_coldkey=neuron.coldkey or None
            ),
            source_sha256=payload.source_sha256,
            tree_sha256=upload.info.tree_sha256,
            source_bytes=upload.raw,
            module=upload.info.module,
            go_version=upload.info.go_version,
            file_count=upload.info.files,
            now=now,
            name=payload.name,
        )
        if verified is not None:
            await insert_generator_payment(
                session, verified=verified, submission_id=row.submission_id
            )
        superseded = await supersede_active(
            session, miner_hotkey=payload.hotkey, keep=row.submission_id, now=now
        )
        await session.commit()
    except PaymentReplayedError:
        await session.rollback()
        raise
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="this exact archive was uploaded concurrently"
        ) from exc
    logger.info(
        "generator submission accepted hotkey=%s submission_id=%s files=%d "
        "superseded=%d paid=%s",
        payload.hotkey,
        row.submission_id,
        row.file_count,
        len(superseded),
        verified is not None,
    )
    return GeneratorSubmissionUploadResponse(
        submission_id=row.submission_id,
        source_sha256=row.source_sha256,
        tree_sha256=row.tree_sha256,
        status="active",
        superseded=superseded,
        name=row.name,
    )


def _check_duplicates_and_cooldown(
    response: Response,
    *,
    existing: GeneratorSubmission | None,
    same_tree: GeneratorSubmission | None,
    last: datetime | None,
    cooldown_seconds: int,
    now: datetime,
) -> None:
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this exact archive was already uploaded as {existing.submission_id}"
            ),
        )
    if same_tree is not None:
        # Re-packing identical files is not a new generator, whoever uploads it.
        raise HTTPException(
            status_code=409,
            detail=(
                "these exact source files were already uploaded as "
                f"{same_tree.submission_id}"
            ),
        )
    if last is not None:
        retry_at = last + timedelta(seconds=cooldown_seconds)
        if retry_at > now:
            retry_after = str(int((retry_at - now).total_seconds()) + 1)
            response.headers["Retry-After"] = retry_after
            raise HTTPException(
                status_code=429,
                detail=f"generator upload cooldown until {retry_at.isoformat()}",
                headers={"Retry-After": retry_after},
            )


@router.get("/submissions", response_model=GeneratorSubmissionListResponse)
async def list_hotkey_submissions(
    session: SessionDep,
    hotkey: Annotated[str, Query(pattern=_SS58)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> GeneratorSubmissionListResponse:
    """A hotkey's recent generators: lifecycle and aggregate outcome only."""
    rows = await list_submissions(session, miner_hotkey=hotkey, limit=limit)
    return GeneratorSubmissionListResponse(
        submissions=await _public_summaries(session, rows)
    )


async def _public_summaries(
    session: AsyncSession, rows: Sequence[GeneratorSubmission]
) -> list[GeneratorSubmissionSummary]:
    ids = [row.submission_id for row in rows]
    latest = await latest_runs(session, ids)
    counts = await case_status_counts(session, [run.run_id for run in latest.values()])
    identities = await generator_identities(session, ids)
    return [
        submission_summary(
            row,
            latest.get(row.submission_id),
            counts.get(latest[row.submission_id].run_id)
            if row.submission_id in latest
            else None,
            identities.get(row.submission_id),
        )
        for row in rows
    ]


@router.get(
    "/submissions/{submission_id}", response_model=PublicGeneratorSubmissionDetail
)
async def public_submission(
    submission_id: UUID, session: SessionDep, response: Response
) -> PublicGeneratorSubmissionDetail:
    """One generator: identity, lifecycle, the hotkey's other versions, and
    its round-by-round results. Never source, seeds, or case content."""
    response.headers["Cache-Control"] = _PUBLIC_READ_CACHE
    row = await get_submission_without_source(session, submission_id)
    if row is None or row.public_default:
        raise HTTPException(status_code=404, detail="unknown generator submission")
    versions = list(
        await list_submissions(session, miner_hotkey=row.miner_hotkey, limit=25)
    )
    if all(other.submission_id != row.submission_id for other in versions):
        versions.append(row)
    summaries = await _public_summaries(session, versions)
    return PublicGeneratorSubmissionDetail(
        submission=next(
            summary
            for summary in summaries
            if summary.submission_id == row.submission_id
        ),
        versions=summaries,
        rounds=await submission_round_views(session, submission_id),
    )


@router.get("/board", response_model=PublicGeneratorBoardResponse)
async def board(
    session: SessionDep, response: Response
) -> PublicGeneratorBoardResponse:
    """The generator ledger validators fold, with each entry's public
    identity (name, avatar, version) and recent round form."""
    response.headers["Cache-Control"] = _PUBLIC_READ_CACHE
    resolved = await load_settings(session)
    now = datetime.now(UTC)
    ledger = await build_ledger(session, resolved=resolved, now=now)
    identities = await generator_identities(
        session, [entry.submission_id for entry in ledger.entries]
    )
    form = await hotkey_round_points(
        session,
        [entry.miner_hotkey for entry in ledger.entries],
        since=now - timedelta(days=resolved.settings.window_days),
        limit=PUBLIC_BOARD_RECENT_ROUNDS,
    )
    entries = []
    for entry in ledger.entries:
        identity = identities.get(entry.submission_id)
        held, recent = form.get(entry.miner_hotkey, (0, []))
        entries.append(
            PublicGeneratorBoardEntry(
                **entry.model_dump(),
                name=identity.name if identity else None,
                name_handle=identity.name_handle if identity else None,
                avatar_url=identity.avatar_url if identity else None,
                version=identity.version if identity else None,
                status=cast("SubmissionStatus", identity.status) if identity else None,
                qualified_at=identity.qualified_at if identity else None,
                rounds_in_window=held,
                recent=recent,
            )
        )
    return PublicGeneratorBoardResponse(
        entries=entries,
        count=ledger.count,
        window_days=ledger.window_days,
        mode=ledger.mode,
        generated_at=ledger.generated_at,
    )


@router.get("/rounds", response_model=GeneratorRoundListResponse)
async def public_rounds(
    request: Request,
    session: SessionDep,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GeneratorRoundListResponse:
    """Recent rounds: commitments, chain anchors, and status.

    A round's secret, seed, and dataset digest appear only after its privacy
    window, so anyone can then check the secret against the commitment
    published before the round opened and recompute every case.
    """
    response.headers["Cache-Control"] = _PUBLIC_READ_CACHE
    resolved = await load_settings(session)
    loop = getattr(request.app.state, "generator_round_loop", None)
    schedule = (
        loop.schedule
        if loop is not None and loop.schedule is not None
        else GeneratorRoundSchedule(
            mode=resolved.settings.mode,
            epochs_per_round=resolved.settings.epochs_per_round,
        )
    )
    return GeneratorRoundListResponse(
        schedule=schedule.model_copy(update={"last_error": None}),
        rounds=await round_summaries(
            session, await list_rounds(session, limit=limit, offset=offset)
        ),
    )


@router.get("/rounds/{round_index}", response_model=GeneratorRoundDetail)
async def public_round(
    round_index: int, session: SessionDep, response: Response
) -> GeneratorRoundDetail:
    """One round: columns and their scores, and agent standings once scored."""
    response.headers["Cache-Control"] = _PUBLIC_READ_CACHE
    row = await get_round(session, round_index, read_view=True)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown round")
    scored = row.status == "scored"
    columns = column_views(row) if row.status != "committed" else []
    generators = await generator_identities(
        session,
        [column.submission_id for column in columns if not column.public_default],
    )
    standings = standing_views(row) if scored else []
    agents = await agent_identities(session, [s.agent_id for s in standings])
    return GeneratorRoundDetail(
        summary=await round_summary(session, row),
        columns=[
            column.model_copy(
                update={
                    "name": identity.name,
                    "name_handle": identity.name_handle,
                    "avatar_url": identity.avatar_url,
                    "version": identity.version,
                }
            )
            if (identity := generators.get(column.submission_id)) is not None
            and not column.public_default
            else column
            for column in columns
        ],
        standings=[
            standing.model_copy(
                update={
                    "agent_name": agent.name,
                    "agent_version": agent.version,
                    "miner_hotkey": agent.miner_hotkey,
                    "avatar_url": agent.avatar_url,
                }
            )
            if (agent := agents.get(standing.agent_id)) is not None
            else standing
            for standing in standings
        ],
    )
