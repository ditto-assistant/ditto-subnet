"""Upload-flow endpoints.

This module ships the pre-payment surface a miner CLI hits before
spending TAO (``/upload/eval-pricing`` + ``/upload/check``) and the
post-payment orchestrator (``/upload/agent``) that re-verifies the
proof on chain, stores the tarball in S3, and writes the matching
``agents`` + ``evaluation_payments`` rows in a single transaction.

Deferred validations (added when their dependencies land):
- tar manifest structure (needs Go-harness interface signatures)
- banned-hotkey check (needs ``banned_hotkeys`` table)
- Go-import allowlist scan (needs the allowlist file)
- schema diff against ``schema/initial_harness.sql`` (needs the file)
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

import bittensor
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
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
    get_payment_verifier,
    get_price_oracle,
    get_session,
    get_storage_client,
)
from ditto.api_server.payment_verifier import (
    PaymentProof,
    PaymentVerifier,
)
from ditto.api_server.pricing import (
    MalformedPriceError,
    PriceOracle,
)
from ditto.api_server.storage import S3StorageClient
from ditto.chain import ChainError
from ditto.db.models import AgentStatus
from ditto.db.queries.agents import insert_agent
from ditto.db.queries.payments import insert_evaluation_payment

if TYPE_CHECKING:
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["upload"])

# `/upload/check` + `/upload/agent` failure codes live in the 1xxx
# agent-side range per CODE-REVIEW-CHECKLIST.md. New codes added here
# go in 110x.
ERROR_CODE_BAD_SIGNATURE = 1100
ERROR_CODE_HOTKEY_NOT_REGISTERED = 1101
ERROR_CODE_TARBALL_TOO_LARGE = 1102
ERROR_CODE_TARBALL_SHA_MISMATCH = 1103

# Hard cap shared with /upload/check. Tarballs above this size are
# rejected; /upload/check enforces it from the miner-reported header,
# /upload/agent enforces it from the actual streamed bytes.
MAX_TARBALL_SIZE_BYTES = 2 * 1024 * 1024

# Streaming read chunk size. 256 KiB keeps memory bounded while letting
# size + sha256 update incrementally without re-reading the body.
_CHUNK_SIZE_BYTES = 256 * 1024

ChainDep = Annotated["ChainClient", Depends(get_chain_client)]
OracleDep = Annotated[PriceOracle, Depends(get_price_oracle)]
PaymentVerifierDep = Annotated[PaymentVerifier, Depends(get_payment_verifier)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/eval-pricing", response_model=EvalPricingResponse)
async def eval_pricing(request: Request, oracle: OracleDep) -> EvalPricingResponse:
    """Quote the current upload fee in rao.

    ``PricingError`` subclasses propagate so the envelope handler in
    :mod:`ditto.api_server.middleware.error_envelope` can attach the
    specific 31xx error code instead of the generic 3002 catch-all.
    """
    config = request.app.state.config
    price_usd = await oracle.get_tao_usd()

    fee_tao = (config.pricing.fee_usd * config.pricing.fee_buffer) / price_usd
    amount_rao = int(fee_tao * Decimal("1e9"))
    if amount_rao <= 0:
        raise MalformedPriceError(f"computed amount_rao is non-positive: {amount_rao}")

    return EvalPricingResponse(
        amount_rao=amount_rao,
        send_address=config.upload_payment_address,
    )


@router.post("/check", response_model=UploadCheckResponse)
async def check(
    request: Request, body: UploadCheckRequest, chain: ChainDep
) -> UploadCheckResponse:
    """Pre-payment dry-run validation.

    Aggregates every failed check into ``error_codes`` + ``messages`` so
    the miner CLI sees every reason in one round trip. ``file_size_bytes``
    is miner-reported and unverified at this endpoint; the next-PR
    ``/upload/agent`` re-derives it from the actual tarball bytes.
    """
    netuid = request.app.state.config.chain.netuid
    codes: list[int] = []
    messages: list[str] = []

    # 1. Signature over UTF-8 bytes of "{hotkey}:{sha256}".
    payload = f"{body.hotkey}:{body.sha256}".encode()
    if not _verify_signature(body.hotkey, payload, body.signature):
        codes.append(ERROR_CODE_BAD_SIGNATURE)
        messages.append("signature did not verify against the hotkey")

    # 2. Hotkey registered. On a chain outage we return 503 instead of
    #    a silent false-pass that would lie to miners.
    try:
        registered = await chain.is_registered(body.hotkey, netuid=netuid)
    except ChainError as e:
        logger.warning(f"chain unreachable during /upload/check: {e}")
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from e
    if not registered:
        codes.append(ERROR_CODE_HOTKEY_NOT_REGISTERED)
        messages.append(f"hotkey is not registered on netuid {netuid}")

    # 3. Tarball size cap.
    if body.file_size_bytes > MAX_TARBALL_SIZE_BYTES:
        codes.append(ERROR_CODE_TARBALL_TOO_LARGE)
        messages.append(f"tarball exceeds {MAX_TARBALL_SIZE_BYTES} bytes")

    return UploadCheckResponse(ok=not codes, error_codes=codes, messages=messages)


@router.post("/agent", response_model=UploadAgentResponse, status_code=200)
async def upload_agent(
    request: Request,
    agent_tar: Annotated[UploadFile, File(description="gzipped tarball, <=2 MB")],
    hotkey: Annotated[str, Form(pattern=_SS58_PATTERN)],
    sha256: Annotated[str, Form(pattern=_SHA256_PATTERN)],
    # The 64-character cap is a chosen value rather than a spec mandate;
    # ``agents.name`` is TEXT in the schema and the cap is the only
    # defense against pathological values polluting logs / dashboards.
    name: Annotated[str, Form(min_length=1, max_length=64)],
    signature: Annotated[str, Form(pattern=_SIGNATURE_HEX_PATTERN)],
    payment_block_hash: Annotated[str, Form(pattern=_BLOCK_HASH_PATTERN)],
    payment_block_number: Annotated[int, Form(ge=1)],
    payment_extrinsic_index: Annotated[int, Form(ge=0)],
    chain: ChainDep,
    verifier: PaymentVerifierDep,
    storage: StorageDep,
    session: SessionDep,
) -> UploadAgentResponse:
    """Full upload submission with proof of payment.

    Ordering is cheap-before-expensive so a rejection costs the API the
    minimum work, and every mutation happens after every validation has
    passed:

    1. Form fields auto-validated by FastAPI regex (already done by
       the time this body runs; malformed input returns 422).
    2. Signature over ``f"{hotkey}:{sha256}"`` (CPU only, no I/O; 1100).
    3. Hotkey registered on the configured netuid (1 Pylon call; 1101).
    4. Stream tar bytes: size cap (1102) + sha256 re-verify (1103).
    5. PaymentVerifier.verify_payment (4 chain calls; 3201-3206).
    6. ``agent_id = uuid4()``.
    7. ``storage.put_object`` (orphan blob is cheap on DB failure;
       orphan agent rows would break the state machine).
    8. Atomic DB tx: ``insert_agent`` + ``insert_evaluation_payment``
       (3207 surfaces here when the PK rejects a replayed proof).
    9. Return ``UploadAgentResponse``.
    """
    netuid = request.app.state.config.chain.netuid

    # 2. Signature verify against the claimed hotkey + sha.
    payload = f"{hotkey}:{sha256}".encode()
    if not _verify_signature(hotkey, payload, signature):
        raise HTTPException(
            status_code=400, detail="signature did not verify against the hotkey"
        )

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

    # 5. Chain-side verification (typed errors are mapped to 3201-3206
    # by the envelope handler; we re-raise unchanged).
    verified = await verifier.verify_payment(
        PaymentProof(
            block_hash=payment_block_hash,
            block_number=payment_block_number,
            extrinsic_index=payment_extrinsic_index,
        ),
        expected_hotkey=hotkey,
    )

    # 6. Server-generated identity. The CLI cannot pre-supply it.
    agent_id = uuid.uuid4()

    # 7. S3 first: orphan blobs are cheap + invisible to the state
    # machine. Orphan agent rows would surface as undownloadable agents
    # in the validator polling flow.
    await storage.put_object(
        key=f"{agent_id}/agent.tar.gz",
        body=tar_bytes,
        content_type="application/gzip",
    )

    # 8. Atomic DB tx: agent + payment commit together or roll back
    # together. A replayed payment proof surfaces as PaymentReplayedError
    # (3207) and the envelope handler maps it to HTTP 402.
    ip_address = request.client.host if request.client else None
    async with session.begin():
        await insert_agent(
            session,
            agent_id=agent_id,
            miner_hotkey=hotkey,
            name=name,
            sha256=sha256,
            ip_address=ip_address,
        )
        await insert_evaluation_payment(session, verified=verified, agent_id=agent_id)

    logger.info(
        f"upload accepted hotkey={hotkey} agent_id={agent_id} "
        f"amount_rao={verified.amount_rao} block_hash={verified.block_hash}"
    )
    return UploadAgentResponse(agent_id=agent_id, status=AgentStatus.UPLOADED)


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
