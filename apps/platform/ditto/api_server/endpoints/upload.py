"""Upload-flow endpoints.

This module ships the pre-payment surface a miner CLI hits before
spending TAO (``/upload/eval-pricing`` + ``/upload/check``) and the
post-payment orchestrator (``/upload/agent``) that re-verifies the
proof on chain, stores the tarball in S3, and writes the matching
``agents`` + ``evaluation_payments`` rows in a single transaction.

Deferred validations (added when their dependencies land):
- declared file/entrypoint manifest (needs the harness-interface spec)
- import / dependency allowlist (needs the allowlist file)
- schema diff against ``schema/initial_harness.sql`` (needs the file)

Gzip magic, tar member safety, and a root ``Dockerfile`` are enforced on
``/upload/agent`` before payment verification or object storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

import bittensor
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import UUID4
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models import (
    EvalPricingResponse,
    UploadAgentResponse,
    UploadCheckRequest,
    UploadCheckResponse,
)
from ditto.api_models.upload import (
    _BLOCK_HASH_PATTERN,
    _SHA256_PATTERN,
    _SIGNATURE_HEX_PATTERN,
    _SS58_PATTERN,
)
from ditto.api_server.dependencies import (
    get_chain_client,
    get_embedder,
    get_payment_verifier,
    get_session,
    get_storage_client,
)
from ditto.api_server.embedding import Embedder
from ditto.api_server.fingerprint import (
    compute_content_fingerprint,
    compute_embedding_input,
    compute_normalized_source_hash,
    compute_prompt_fingerprint,
)
from ditto.api_server.payment_verifier import (
    PaymentProof,
    PaymentRecoveryExpired,
    PaymentReplayedError,
    PaymentVerifier,
)
from ditto.api_server.pricing.errors import UnsupportedFeeDenominationError
from ditto.api_server.source_inspect import (
    SourceInspectError,
    validate_upload_archive,
)
from ditto.api_server.storage import S3StorageClient
from ditto.api_server.upload_feedback import submission_cooldown_message
from ditto.chain import ChainError
from ditto.db.models import AgentStatus, UploadAdmissionReservation
from ditto.db.queries.agents import (
    SubmissionCooldownError,
    get_submission_retry_at,
    insert_agent,
)
from ditto.db.queries.bans import is_hotkey_banned
from ditto.db.queries.name_claims import upload_name_is_reserved
from ditto.db.queries.payments import (
    consume_evaluation_credit,
    get_agent_for_payment_proof,
    get_evaluation_payment_for_proof,
    get_same_owner_agent_by_sha,
    insert_evaluation_payment,
)
from ditto.db.queries.submission_settings import (
    UPLOAD_ADMISSION_TTL,
    EffectiveSubmissionSettings,
    consume_or_enforce_upload_admission,
    effective_submission_settings,
    get_upload_admission,
    get_upload_admission_for_coldkey,
    release_upload_admission_for_exact_retry,
    reservation_send_address,
    reserve_upload_admission,
)

if TYPE_CHECKING:
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

_UPLOAD_ARCHIVE_DETAILS = {
    "archive-not-gzip": "archive is not a gzip-compressed tar",
    "archive-unreadable": "archive is not a readable gzip-compressed tar",
    "artifact-too-many-members": "archive contains too many members",
    "artifact-too-large": "archive expands beyond the safety limit",
    "archive-unsafe-path": "archive contains an unsafe path",
    "archive-duplicate-path": "archive contains a duplicate path",
    "archive-special-file": "archive contains a link or special file",
    "archive-missing-dockerfile": "Dockerfile is missing from the archive root",
    "archive-dockerfile-unreadable": "Dockerfile is not valid UTF-8 text",
}

router = APIRouter(prefix="/upload", tags=["upload"])

UPLOAD_SIGNATURE_MAX_SKEW_SECONDS = 300

# `/upload/check` + `/upload/agent` failure codes live in the 1xxx
# agent-side range per CODE-REVIEW-CHECKLIST.md. New codes added here
# go in 110x.
ERROR_CODE_BAD_SIGNATURE = 1100
ERROR_CODE_HOTKEY_NOT_REGISTERED = 1101
ERROR_CODE_TARBALL_TOO_LARGE = 1102
ERROR_CODE_HOTKEY_BANNED = 1103
ERROR_CODE_IDENTICAL_SUBMISSION = 1104
ERROR_CODE_SUBMISSION_COOLDOWN = 1105

DEFAULT_MAX_TARBALL_SIZE_BYTES = 20 * 1024 * 1024


def _tarball_size_cap_from_env() -> int:
    """Return upload cap, keeping the competition default explicit.

    Rust starter-kit submissions with bundled model fixtures are expected to
    stay below the launch cap. Operators may still override it explicitly for
    local/dev runs or emergency changes.
    """
    raw = os.environ.get("DITTO_MAX_TARBALL_SIZE_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_TARBALL_SIZE_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "invalid DITTO_MAX_TARBALL_SIZE_BYTES=%r; falling back to 20 MiB",
            raw,
        )
        return DEFAULT_MAX_TARBALL_SIZE_BYTES
    if value <= 0:
        logger.warning(
            "non-positive DITTO_MAX_TARBALL_SIZE_BYTES=%r; falling back to 20 MiB",
            raw,
        )
        return DEFAULT_MAX_TARBALL_SIZE_BYTES
    return value


@dataclass(frozen=True)
class _PaymentTerms:
    """What a payment must match, snapshotted before any session rollback.

    Shared by ``/upload/check`` recovery and ``/upload/agent`` so the two can
    never disagree about a reserved quote. With a reservation, its fee,
    destination and amnesty bind payments finalized before
    ``reserved_terms_expire_at``; a later payment must match the current fee,
    which is ``None`` (fail closed) when the current revision is unquotable.

    Reading the terms never refuses an unquotable revision: many branches
    charge no current fee (an issued reservation, a recorded credit, an exact
    retry, an already-recorded payment, a validation failure). The single
    fail-closed gate is ``_quoted_amount``, called only where a payment is
    verified against the current fee, plus ``reserve_upload_admission`` for a
    new quote and the verifier's post-expiry fallback.
    """

    settings: EffectiveSubmissionSettings
    expected_amount_rao: int | None
    """``None`` only with no reservation and an unquotable revision: verifying
    a payment must then fail closed (``_quoted_amount``)."""
    legacy_amount_cutoff_at: datetime | None
    expected_send_address: str
    reserved_terms_expire_at: datetime | None
    fallback_amount_rao: int | None
    fallback_send_address: str


async def _payment_terms(
    session: AsyncSession,
    *,
    reservation: UploadAdmissionReservation | None,
    default_payment_address: str,
) -> _PaymentTerms:
    """Snapshot the payment terms without refusing an unquotable revision."""
    settings = await effective_submission_settings(
        session,
        default_payment_address=default_payment_address,
        require_quotable=False,
    )
    current_fee_rao = settings.fee_amount_rao if settings.quotable else None
    if reservation is None:
        return _PaymentTerms(
            settings=settings,
            expected_amount_rao=current_fee_rao,
            legacy_amount_cutoff_at=None,
            expected_send_address=settings.payment_address,
            reserved_terms_expire_at=None,
            fallback_amount_rao=current_fee_rao,
            fallback_send_address=settings.payment_address,
        )
    return _PaymentTerms(
        settings=settings,
        expected_amount_rao=reservation.fee_amount_rao,
        legacy_amount_cutoff_at=reservation.legacy_payment_cutoff_at,
        expected_send_address=reservation_send_address(
            reservation, effective_address=settings.payment_address
        ),
        reserved_terms_expire_at=_as_utc(reservation.expires_at),
        fallback_amount_rao=current_fee_rao,
        fallback_send_address=settings.payment_address,
    )


def _quoted_amount(terms: _PaymentTerms) -> int:
    """The amount a payment must match; fails closed when nothing is quotable."""
    if terms.expected_amount_rao is None:
        raise UnsupportedFeeDenominationError(
            "no reservation binds this payment and the current submission fee "
            "cannot be quoted"
        )
    return terms.expected_amount_rao


# Hard cap shared with /upload/check. Tarballs above this size are
# rejected; /upload/check enforces it from the miner-reported header,
# /upload/agent enforces it from the actual streamed bytes.
MAX_TARBALL_SIZE_BYTES = _tarball_size_cap_from_env()

# Streaming read chunk size. 256 KiB keeps memory bounded while letting
# size + sha256 update incrementally without re-reading the body.
_CHUNK_SIZE_BYTES = 256 * 1024


def _as_utc(timestamp: datetime) -> datetime:
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp


def _ensure_payment_recovery_fresh(block_timestamp: datetime) -> None:
    timestamp = _as_utc(block_timestamp)
    if timestamp + UPLOAD_ADMISSION_TTL <= datetime.now(UTC):
        raise PaymentRecoveryExpired(
            f"payment at {timestamp.isoformat()} is outside the 24-hour window"
        )


ChainDep = Annotated["ChainClient", Depends(get_chain_client)]
PaymentVerifierDep = Annotated[PaymentVerifier, Depends(get_payment_verifier)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/eval-pricing", response_model=EvalPricingResponse)
async def eval_pricing(request: Request, session: SessionDep) -> EvalPricingResponse:
    """Quote the operator-controlled, TAO-denominated upload fee."""
    config = request.app.state.config
    settings = await effective_submission_settings(
        session, default_payment_address=config.upload_payment_address
    )

    return EvalPricingResponse(
        amount_rao=settings.fee_amount_rao,
        send_address=settings.payment_address,
    )


@router.post("/check", response_model=UploadCheckResponse)
async def check(
    request: Request,
    body: UploadCheckRequest,
    chain: ChainDep,
    verifier: PaymentVerifierDep,
    session: SessionDep,
) -> UploadCheckResponse:
    """Pre-payment dry-run validation.

    Aggregates every failed check into ``error_codes`` + ``messages`` so
    the miner CLI sees every reason in one round trip. ``file_size_bytes``
    is miner-reported and unverified at this endpoint; the next-PR
    ``/upload/agent`` re-derives it from the actual tarball bytes.
    """
    chain_config = request.app.state.config.chain
    netuid = chain_config.netuid
    codes: list[int] = []
    messages: list[str] = []

    # 1. Verify the short-lived, request-specific hotkey signature.
    signature_valid = _verify_upload_signature(
        body.hotkey,
        body.sha256,
        body.signature,
        body.signature_timestamp,
        body.signature_nonce,
    )
    if not signature_valid:
        codes.append(ERROR_CODE_BAD_SIGNATURE)
        messages.append("signature did not verify against the hotkey")

    # 2. Hotkey registered. On a chain outage we return 503 instead of
    #    a silent false-pass that would lie to miners.
    try:
        owner_coldkey = await chain.get_registered_coldkey(body.hotkey, netuid=netuid)
    except ChainError as e:
        logger.warning(f"chain unreachable during /upload/check: {e}")
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from e
    registered = owner_coldkey is not None
    if not registered:
        codes.append(ERROR_CODE_HOTKEY_NOT_REGISTERED)
        messages.append(f"hotkey is not registered on netuid {netuid}")

    # 3. Tarball size cap.
    if body.file_size_bytes > MAX_TARBALL_SIZE_BYTES:
        codes.append(ERROR_CODE_TARBALL_TOO_LARGE)
        messages.append(f"tarball exceeds {MAX_TARBALL_SIZE_BYTES} bytes")

    # 4. Hotkey-level ban. Reported here (dry run) so a banned miner learns it
    #    before spending TAO; /upload/agent enforces it as a hard 403.
    banned = await is_hotkey_banned(session, hotkey=body.hotkey)
    if banned:
        codes.append(ERROR_CODE_HOTKEY_BANNED)
        messages.append("hotkey is banned from submitting")

    # 5. Stop the common accidental duplicate before the miner pays. A second
    # independently seeded run remains available, but it must be explicit.
    duplicate = None
    if (
        signature_valid
        and owner_coldkey is not None
        and not banned
        and not body.allow_identical_rescore
        and body.payment_block_hash is None
    ):
        duplicate = await get_same_owner_agent_by_sha(
            session, miner_coldkey=owner_coldkey, sha256=body.sha256
        )
        if duplicate:
            codes.append(ERROR_CODE_IDENTICAL_SUBMISSION)
            messages.append(
                "The previous submission cannot be resubmitted. "
                "Please try again after updating."
            )

    reserved_admission = (
        await get_upload_admission_for_coldkey(session, miner_coldkey=owner_coldkey)
        if owner_coldkey is not None
        else None
    )
    # Recovery honours the reservation for a payment finalized before it
    # expired, even when the reservation has expired by now (or the current
    # revision is unquotable); the verifier compares the payment's block time
    # with this expiry and otherwise requires the current fee.
    # Only this hotkey's reservation can bind its payment, as in /upload/agent
    # (the archive may differ: recovery rotates it). Another hotkey's
    # reservation on the same coldkey never lends it a quoted fee.
    own_reservation = (
        reserved_admission
        if reserved_admission is not None
        and reserved_admission.miner_hotkey == body.hotkey
        else None
    )
    recovery_terms = await _payment_terms(
        session,
        reservation=own_reservation,
        default_payment_address=request.app.state.config.upload_payment_address,
    )
    settings = recovery_terms.settings
    recovery_payment_verified = False
    proof_already_used = False
    recovery_paid_at: datetime | None = None
    if (
        not codes
        and body.payment_block_hash is not None
        and body.payment_block_number is not None
        and body.payment_extrinsic_index is not None
    ):
        payment_record = await get_evaluation_payment_for_proof(
            session,
            block_hash=body.payment_block_hash,
            extrinsic_index=body.payment_extrinsic_index,
        )
        if payment_record is not None:
            if payment_record.agent_id is not None:
                existing = await get_agent_for_payment_proof(
                    session,
                    block_hash=body.payment_block_hash,
                    extrinsic_index=body.payment_extrinsic_index,
                )
                if not (
                    existing is not None
                    and existing.miner_hotkey == body.hotkey
                    and existing.sha256 == body.sha256
                ):
                    raise PaymentReplayedError("payment proof already used")
                # The proof is consumed: it funded this exact upload already,
                # so it funds nothing new. No new payment is needed (the
                # /upload/agent exact retry returns the existing agent), but
                # the slot is reserved like any fresh request: the cooldown
                # applies and no live reservation is rotated or taken over.
                proof_already_used = True
            elif payment_record.miner_hotkey != body.hotkey:
                raise PaymentReplayedError(
                    "payment credit belongs to a different hotkey"
                )
            elif payment_record.miner_coldkey != owner_coldkey:
                # Same rule as /upload/agent's credit path: a credit is bound
                # to the coldkey that paid it.
                raise PaymentReplayedError(
                    "payment credit belongs to a different coldkey"
                )
            else:
                _ensure_payment_recovery_fresh(payment_record.timestamp)
                recovery_payment_verified = True
                recovery_paid_at = payment_record.timestamp
        else:
            # The replay lookup autobegins a read transaction. Do not hold a
            # pooled connection across the recovery proof's chain reads.
            if session.in_transaction():
                rollback_result = session.rollback()
                if inspect.isawaitable(rollback_result):
                    await rollback_result
            recovery_amount_rao = _quoted_amount(recovery_terms)
            try:
                verified = await verifier.verify_payment(
                    PaymentProof(
                        block_hash=body.payment_block_hash,
                        block_number=body.payment_block_number,
                        extrinsic_index=body.payment_extrinsic_index,
                    ),
                    expected_hotkey=body.hotkey,
                    expected_amount_rao=recovery_amount_rao,
                    legacy_amount_cutoff_at=recovery_terms.legacy_amount_cutoff_at,
                    expected_send_address=recovery_terms.expected_send_address,
                    reserved_terms_expire_at=recovery_terms.reserved_terms_expire_at,
                    fallback_amount_rao=recovery_terms.fallback_amount_rao,
                    fallback_send_address=recovery_terms.fallback_send_address,
                )
            except ChainError as e:
                logger.warning(f"chain unreachable during /upload/check recovery: {e}")
                raise HTTPException(
                    status_code=503, detail="chain unavailable; retry shortly"
                ) from e
            _ensure_payment_recovery_fresh(verified.block_timestamp)
            recovery_payment_verified = True
            recovery_paid_at = verified.block_timestamp
            if verified.miner_coldkey != owner_coldkey:
                raise PaymentReplayedError(
                    "payment owner no longer matches this admission reservation"
                )

    retry_at = None
    if (
        duplicate is None
        and owner_coldkey is not None
        and not recovery_payment_verified
    ):
        retry_at = await get_submission_retry_at(
            session,
            miner_coldkey=owner_coldkey,
            cooldown=timedelta(seconds=settings.cooldown_seconds),
        )
        if retry_at is not None:
            codes.append(ERROR_CODE_SUBMISSION_COOLDOWN)
            messages.append(submission_cooldown_message(retry_at))

    admission = None
    if not codes and body.reserve_submission_slot:
        assert owner_coldkey is not None
        if session.in_transaction():
            rollback_result = session.rollback()
            if inspect.isawaitable(rollback_result):
                await rollback_result
        try:
            async with session.begin():
                # Not required quotable here: an existing reservation keeps its
                # issued fee, and reserve_upload_admission itself refuses to
                # issue a new quote from an unquotable revision.
                settings = await effective_submission_settings(
                    session,
                    default_payment_address=request.app.state.config.upload_payment_address,
                    require_quotable=False,
                )
                admission = await reserve_upload_admission(
                    session,
                    miner_coldkey=owner_coldkey,
                    miner_hotkey=body.hotkey,
                    sha256=body.sha256,
                    settings=settings,
                    replace_existing=recovery_payment_verified,
                    paid_at=recovery_paid_at,
                )
        except SubmissionCooldownError as exc:
            retry_at = exc.retry_at
            codes.append(ERROR_CODE_SUBMISSION_COOLDOWN)
            messages.append(submission_cooldown_message(retry_at))

    payment_required = (
        not codes and not recovery_payment_verified and not proof_already_used
    )
    return UploadCheckResponse(
        ok=not codes,
        error_codes=codes,
        messages=messages,
        payment_required=payment_required,
        identical_agent_id=duplicate.agent_id if duplicate else None,
        identical_agent_status=duplicate.status if duplicate else None,
        retry_at=retry_at,
        admission_token=admission.token if admission else None,
        admission_expires_at=admission.expires_at if admission else None,
        cooldown_seconds=settings.cooldown_seconds,
        payment_amount_rao=(
            admission.fee_amount_rao
            if admission is not None and payment_required
            else None
        ),
        payment_send_address=(
            admission.payment_send_address
            if admission is not None and payment_required
            else None
        ),
        # Always reported, not only alongside 1101. A client that acts on
        # chain must bind to the deployment's own target rather than to its
        # local configuration; the disagreement it guards against is
        # invisible when the hotkey is unregistered on both subnets.
        netuid=netuid,
        subtensor_network=chain_config.subtensor_network,
    )


@router.post(
    "/agent",
    response_model=UploadAgentResponse,
    response_model_exclude_defaults=True,
    status_code=200,
)
async def upload_agent(
    request: Request,
    agent_tar: Annotated[UploadFile, File(description="gzipped tarball, <=20 MB")],
    hotkey: Annotated[str, Form(pattern=_SS58_PATTERN)],
    sha256: Annotated[str, Form(pattern=_SHA256_PATTERN)],
    # The 64-character cap is a chosen value rather than a spec mandate;
    # ``agents.name`` is TEXT in the schema and the cap is the only
    # defense against pathological values polluting logs / dashboards.
    name: Annotated[str, Form(min_length=1, max_length=64)],
    signature: Annotated[str, Form(pattern=_SIGNATURE_HEX_PATTERN)],
    signature_timestamp: Annotated[int, Form(ge=1, le=2**63 - 1)],
    signature_nonce: Annotated[UUID4, Form()],
    payment_block_hash: Annotated[str, Form(pattern=_BLOCK_HASH_PATTERN)],
    payment_block_number: Annotated[int, Form(ge=1)],
    payment_extrinsic_index: Annotated[int, Form(ge=0)],
    chain: ChainDep,
    verifier: PaymentVerifierDep,
    storage: StorageDep,
    embedder: EmbedderDep,
    session: SessionDep,
    allow_identical_rescore: Annotated[bool, Form()] = False,
    admission_token: Annotated[uuid.UUID | None, Form()] = None,
) -> UploadAgentResponse:
    """Full upload submission with proof of payment.

    Ordering is cheap-before-expensive so a rejection costs the API the
    minimum work, and every mutation happens after every validation has
    passed:

    1. Form fields auto-validated by FastAPI regex (already done by
       the time this body runs; malformed input returns 422).
    2. Fresh domain-separated upload signature (CPU only, no I/O; 400).
    3. Hotkey registered on the configured netuid (1 Pylon call;
       400 if absent, 503 if chain unreachable).
    4. Stream tar bytes: size cap (413) + sha256 re-verify (400).
    5. Resolve the proof as an idempotent retry or an available duplicate-upload
       credit. Reusing an assigned proof for different upload data remains a
       3207 replay rejection.
    6. For a fresh proof, run ``PaymentVerifier.verify_payment`` (5 chain calls;
       3201-3206 on payment rejection, 503 if chain unreachable).
    7. Detect byte-identical source under the immutable payment-time coldkey.
       Unless explicitly opted into another seed, preserve the fresh proof as a
       reusable credit and return the original agent without storing a new one.
    8. ``agent_id = uuid4()`` and ``storage.put_object`` (an orphan blob is cheap
       on DB failure;
       orphan agent rows would break the state machine), then compute the
       content fingerprint (best-effort; ``None`` on an unreadable tarball).
    9. Atomic DB tx: insert the agent and either assign a fresh proof or consume
       the locked credit (3207 surfaces if another request won the proof race).
    10. Return ``UploadAgentResponse``.
    """
    netuid = request.app.state.config.chain.netuid

    # 2. Signature verify against the claimed hotkey, sha, time, and nonce.
    if not _verify_upload_signature(
        hotkey, sha256, signature, signature_timestamp, signature_nonce
    ):
        raise HTTPException(
            status_code=400, detail="signature did not verify against the hotkey"
        )

    # 2b. Hotkey-level ban. Checked right after the (CPU-only) signature proves
    #     the caller owns the hotkey and before any chain/payment/storage work,
    #     so a banned miner is rejected as cheaply as possible.
    if await is_hotkey_banned(session, hotkey=hotkey):
        raise HTTPException(status_code=403, detail="hotkey is banned from submitting")

    # The ban check autobegan a transaction on the pooled session. End it NOW:
    # nothing until the atomic insert (step 8) touches the database, and holding
    # a checked-out connection across the slow middle — streaming the tarball
    # from a possibly-slow miner, chain payment verification, the storage write,
    # and the CPU-bound fingerprint computes — starves the pool under concurrent
    # uploads (the 2026-07-16 outage: idle-in-transaction sessions pinned every
    # slot while requests queued 30s for a connection).
    if session.in_transaction():
        rollback_result = session.rollback()
        if inspect.isawaitable(rollback_result):
            await rollback_result

    # 3. Hotkey must be registered on this subnet. Chain outage surfaces
    # as 503; falling through would silently accept off-subnet hotkeys.
    try:
        registered = await chain.is_registered(hotkey, netuid=netuid)
    except ChainError as e:
        logger.warning(f"chain unreachable during /upload/agent: {e}")
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from e
    if not registered:
        raise HTTPException(
            status_code=400, detail=f"hotkey not registered on netuid {netuid}"
        )

    # 4. Stream the tar; enforce size cap + recompute sha256 on bytes.
    tar_bytes, actual_sha = await _read_tar_capped_with_sha(
        agent_tar, MAX_TARBALL_SIZE_BYTES
    )
    if actual_sha != sha256:
        raise HTTPException(
            status_code=400, detail="sha256 of received tarball does not match claim"
        )

    # 5. An upstream/proxy failure can hide the 200 after the original atomic
    # commit. Authenticate and re-hash first, then recover only an *exact* retry.
    # The payment proof remains non-transferable: changing hotkey, name, or bytes
    # keeps the existing 3207 replay rejection.
    existing = await get_agent_for_payment_proof(
        session,
        block_hash=payment_block_hash,
        extrinsic_index=payment_extrinsic_index,
    )
    if existing:
        if (
            existing.miner_hotkey == hotkey
            and existing.name == name
            and existing.sha256 == sha256
        ):
            assert existing.version is not None
            existing_agent_id = existing.agent_id
            existing_version = existing.version
            existing_status = existing.status
            if admission_token is not None:
                await release_upload_admission_for_exact_retry(
                    session,
                    token=admission_token,
                    miner_hotkey=hotkey,
                    sha256=sha256,
                )
                await session.commit()
            logger.info(
                "upload retry recovered hotkey=%s agent_id=%s version=%s block_hash=%s",
                hotkey,
                existing_agent_id,
                existing_version,
                payment_block_hash,
            )
            return UploadAgentResponse(
                agent_id=existing_agent_id,
                version=existing_version,
                status=existing_status,
            )
        raise PaymentReplayedError("payment proof already used by a different upload")

    payment_record = await get_evaluation_payment_for_proof(
        session,
        block_hash=payment_block_hash,
        extrinsic_index=payment_extrinsic_index,
    )
    if payment_record and payment_record.agent_id is not None:
        raise PaymentReplayedError("payment proof already used by a different upload")
    if payment_record and payment_record.miner_hotkey != hotkey:
        raise PaymentReplayedError("payment credit belongs to a different hotkey")
    if payment_record:
        _ensure_payment_recovery_fresh(payment_record.timestamp)
    using_credit = bool(payment_record)
    credit_owner_coldkey = payment_record.miner_coldkey if payment_record else None
    admission = (
        await get_upload_admission(session, token=admission_token)
        if admission_token is not None
        else None
    )
    if admission is not None and (
        admission.miner_hotkey != hotkey or admission.sha256 != sha256
    ):
        admission = None
    # A reserved quote binds the fee it was issued at, so a later pricing
    # revision cannot invalidate it -- for payments finalized within the quote's
    # lifetime. The payment's block time (not this upload's arrival) decides:
    # the verifier applies these reserved terms only when the payment predates
    # ``reserved_terms_expire_at`` and otherwise requires the current fee.
    # Honouring an already-issued quote, redeeming a recorded credit, or
    # rejecting an invalid archive never depends on the current revision being
    # quotable; only charging the current fee does (``_quoted_amount``).
    terms = await _payment_terms(
        session,
        reservation=admission,
        default_payment_address=request.app.state.config.upload_payment_address,
    )

    # The replay lookup autobegan a read transaction. Release that pooled
    # connection before the slow chain/storage/fingerprint work below.
    if session.in_transaction():
        rollback_result = session.rollback()
        if inspect.isawaitable(rollback_result):
            await rollback_result

    # Structural checks run before chain payment verification and storage, so a
    # junk or hostile archive never occupies a screener slot or an object.
    # Exact retries of an already accepted upload returned above.
    try:
        await asyncio.to_thread(validate_upload_archive, tar_bytes)
    except SourceInspectError as exc:
        raise HTTPException(
            status_code=400, detail=_UPLOAD_ARCHIVE_DETAILS.get(exc.code, exc.code)
        ) from exc

    # 6. Chain-side verification. Typed PaymentVerifierError subclasses
    # are mapped to 3201-3206 by the envelope handler; we re-raise them
    # unchanged. A bare ChainError surfaces when one of the verifier's
    # five chain reads cannot reach the configured backends, which we treat as
    # a 503 to match the shipped /upload/check pattern around
    # chain.is_registered.
    verified = None
    if using_credit:
        assert credit_owner_coldkey is not None
        owner_coldkey = credit_owner_coldkey
    else:
        try:
            verified = await verifier.verify_payment(
                PaymentProof(
                    block_hash=payment_block_hash,
                    block_number=payment_block_number,
                    extrinsic_index=payment_extrinsic_index,
                ),
                expected_hotkey=hotkey,
                expected_amount_rao=_quoted_amount(terms),
                legacy_amount_cutoff_at=terms.legacy_amount_cutoff_at,
                expected_send_address=terms.expected_send_address,
                reserved_terms_expire_at=terms.reserved_terms_expire_at,
                fallback_amount_rao=terms.fallback_amount_rao,
                fallback_send_address=terms.fallback_send_address,
            )
        except ChainError as e:
            logger.warning(f"chain unreachable during /upload/agent verify: {e}")
            raise HTTPException(
                status_code=503, detail="chain unavailable; retry shortly"
            ) from e
        _ensure_payment_recovery_fresh(verified.block_timestamp)
        owner_coldkey = verified.miner_coldkey

    duplicate = await get_same_owner_agent_by_sha(
        session, miner_coldkey=owner_coldkey, sha256=sha256
    )
    if duplicate and not allow_identical_rescore:
        assert duplicate.version is not None
        duplicate_agent_id = duplicate.agent_id
        duplicate_version = duplicate.version
        duplicate_status = duplicate.status
        if not using_credit:
            assert verified is not None
            if session.in_transaction():
                rollback_result = session.rollback()
                if inspect.isawaitable(rollback_result):
                    await rollback_result
            try:
                async with session.begin():
                    await insert_evaluation_payment(
                        session,
                        verified=verified,
                        credit_for_agent_id=duplicate_agent_id,
                    )
            except PaymentReplayedError:
                raced = await get_evaluation_payment_for_proof(
                    session,
                    block_hash=payment_block_hash,
                    extrinsic_index=payment_extrinsic_index,
                )
                if not (
                    raced and raced.agent_id is None and raced.miner_hotkey == hotkey
                ):
                    raise
        return UploadAgentResponse(
            agent_id=duplicate_agent_id,
            version=duplicate_version,
            status=duplicate_status,
            payment_disposition="reusable_credit",
            credit_for_agent_id=duplicate_agent_id,
        )

    # The duplicate lookup autobegan a read transaction. Do not pin a pooled
    # connection while uploading to storage and computing fingerprints.
    if session.in_transaction():
        rollback_result = session.rollback()
        if inspect.isawaitable(rollback_result):
            await rollback_result

    # 7. Server-generated identity. The CLI cannot pre-supply it.
    agent_id = uuid.uuid4()

    # 8. S3 first: orphan blobs are cheap + invisible to the state
    # machine. Orphan agent rows would surface as undownloadable agents
    # in the validator polling flow.
    await storage.put_object(
        key=f"{agent_id}/agent.tar.gz",
        body=tar_bytes,
        content_type="application/gzip",
    )

    # 7b. Content fingerprint for the anti-copy gate's content-level signal.
    # Computed only now, on an upload that has passed every check, so a rejected
    # submission never pays the unpack cost. Offloaded to a worker thread because
    # it is CPU-bound (gunzip + shingle-hash the whole tree) and would otherwise
    # block the event loop for every concurrent request. Best-effort: an
    # unreadable/empty tarball yields None (the gate then relies on sha256 + size),
    # never a 500.
    content_fingerprint = await asyncio.to_thread(
        compute_content_fingerprint, tar_bytes
    )
    # 7c. exact-repack hash: the canonicalized-source equality signal for the
    # gate (comments/whitespace stripped, files sorted). Same CPU-bound offload +
    # best-effort None contract as the lexical fingerprint above.
    normalized_source_hash = await asyncio.to_thread(
        compute_normalized_source_hash, tar_bytes
    )
    # 7d. prompt-surface sketch (shadow mode): stored for every agent for
    # calibration/retroactive analysis; not yet a hold trigger. Same offload +
    # best-effort None contract.
    prompt_fingerprint = await asyncio.to_thread(compute_prompt_fingerprint, tar_bytes)
    # 7e. code embedding (shadow mode): build the canonical input (CPU-bound,
    # offloaded) then embed via the self-hosted service. Disabled by default
    # (null embedder -> None) and best-effort: a slow/unreachable embedder yields a
    # null vector rather than failing the upload. The provenance tag is stored so a
    # model change can drive a re-embed sweep and the gate compares only same-model
    # vectors.
    embed_input = await asyncio.to_thread(compute_embedding_input, tar_bytes)
    code_embedding = await embedder.embed(embed_input) if embed_input else None
    code_embed_model = embedder.model_tag if code_embedding is not None else None

    if session.in_transaction():
        rollback_result = session.rollback()
        if inspect.isawaitable(rollback_result):
            await rollback_result

    # 9. Atomic DB tx: agent + payment commit together or roll back
    # together. A replayed payment proof surfaces as PaymentReplayedError
    # (3207) and the envelope handler maps it to HTTP 402.
    paid_at = (
        verified.block_timestamp
        if verified is not None
        else payment_record.timestamp
        if payment_record is not None
        else None
    )
    try:
        async with session.begin():
            # Only the cooldown is read here; the fee was already settled by
            # verification, so an unquotable revision must not refuse an upload
            # whose payment honoured an issued quote.
            settings = await effective_submission_settings(
                session,
                default_payment_address=request.app.state.config.upload_payment_address,
                require_quotable=False,
            )
            await consume_or_enforce_upload_admission(
                session,
                miner_coldkey=owner_coldkey,
                miner_hotkey=hotkey,
                sha256=sha256,
                admission_token=admission_token,
                settings=settings,
                paid_at=paid_at,
            )
            reserved = await upload_name_is_reserved(
                session,
                netuid=netuid,
                agent_name=name,
                miner_hotkey=hotkey,
                miner_coldkey=owner_coldkey,
            )
            if reserved is not None:
                raise HTTPException(status_code=409, detail=reserved)
            version = await insert_agent(
                session,
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                sha256=sha256,
                size_bytes=len(tar_bytes),
                content_fingerprint=content_fingerprint,
                normalized_source_hash=normalized_source_hash,
                prompt_fingerprint=prompt_fingerprint,
                code_embedding=code_embedding,
                code_embed_model=code_embed_model,
            )
            if using_credit:
                locked_credit = await get_evaluation_payment_for_proof(
                    session,
                    block_hash=payment_block_hash,
                    extrinsic_index=payment_extrinsic_index,
                    for_update=True,
                )
                if locked_credit is None:
                    raise PaymentReplayedError("payment credit disappeared")
                await consume_evaluation_credit(
                    session,
                    payment=locked_credit,
                    agent_id=agent_id,
                    miner_hotkey=hotkey,
                )
            else:
                assert verified is not None
                await insert_evaluation_payment(
                    session, verified=verified, agent_id=agent_id
                )
    except PaymentReplayedError:
        # A concurrent identical retry may have passed the first lookup before
        # the winning request committed. The transaction context has rolled this
        # request back, so perform one final exact-identity lookup.
        existing = await get_agent_for_payment_proof(
            session,
            block_hash=payment_block_hash,
            extrinsic_index=payment_extrinsic_index,
        )
        if existing and (
            existing.miner_hotkey == hotkey
            and existing.name == name
            and existing.sha256 == sha256
        ):
            assert existing.version is not None
            return UploadAgentResponse(
                agent_id=existing.agent_id,
                version=existing.version,
                status=existing.status,
            )
        raise

    logger.info(
        f"upload accepted hotkey={hotkey} agent_id={agent_id} version={version} "
        f"payment={'credit' if using_credit else 'fresh'} "
        f"block_hash={payment_block_hash}"
    )
    return UploadAgentResponse(
        agent_id=agent_id,
        version=version,
        status=AgentStatus.UPLOADED,
        payment_disposition="credit_consumed" if using_credit else "consumed",
    )


def _verify_upload_signature(
    hotkey: str,
    sha256: str,
    signature_hex: str,
    signature_timestamp: int,
    signature_nonce: uuid.UUID,
) -> bool:
    """Reject stale or future upload proofs before verifying their signature."""
    if (
        signature_nonce.version != 4
        or abs(time.time() - signature_timestamp) > UPLOAD_SIGNATURE_MAX_SKEW_SECONDS
    ):
        return False
    payload = (
        f"ditto-upload-v2:{hotkey}:{sha256}:{signature_timestamp}:{signature_nonce}"
    ).encode("ascii")
    return _verify_signature(hotkey, payload, signature_hex)


def _verify_signature(hotkey: str, payload: bytes, signature_hex: str) -> bool:
    """Return True iff the signature is a valid sr25519 sig over ``payload``.

    Narrow exception catch on purpose: ``ValueError`` covers malformed
    hex + malformed SS58, ``TypeError`` covers wrong-shape inputs from
    the wallet library. Other exception types are programming bugs that
    should crash the handler so the envelope catch-all returns a 500
    instead of silently reporting "signature did not verify".
    """
    try:
        keypair = bittensor.Keypair(ss58_address=hotkey)
        return bool(keypair.verify(payload, bytes.fromhex(signature_hex)))
    except (ValueError, TypeError):
        return False


async def _read_tar_capped_with_sha(
    upload: UploadFile, max_bytes: int
) -> tuple[bytes, str]:
    """Stream the upload chunk-by-chunk, enforcing size cap + computing sha256.

    Returns the bytes plus the lowercase-hex sha256 of those bytes. The
    accumulating buffer is bounded at ``max_bytes`` so an attacker
    cannot exhaust memory by streaming forever; the cap also keeps the
    happy-path footprint at the documented 2 MB ceiling.

    Raises:
        HTTPException: ``413`` when the streamed body exceeds the cap
            (mapped to ERROR_CODE_TARBALL_TOO_LARGE upstream of this
            function in the route).
    """
    sha = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(_CHUNK_SIZE_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413, detail=f"tarball exceeds {max_bytes} bytes"
            )
        sha.update(chunk)
        chunks.append(chunk)
    return b"".join(chunks), sha.hexdigest()
