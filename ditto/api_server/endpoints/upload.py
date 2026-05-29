"""Upload-flow endpoints.

This module ships the pre-payment surface a miner CLI hits before
spending TAO. ``/upload/eval-pricing`` quotes the current fee in rao;
``/upload/check`` runs the validations that do not require the tarball
bytes themselves.

Deferred validations (added when their dependencies land):
- tar manifest structure (needs Go-harness interface signatures)
- banned-hotkey check (needs ``banned_hotkeys`` table)
- Go-import allowlist scan (needs the allowlist file)
- schema diff against ``schema/initial_harness.sql`` (needs the file)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated

import bittensor
from fastapi import APIRouter, Depends, HTTPException, Request

from ditto.api_models import (
    EvalPricingResponse,
    UploadCheckRequest,
    UploadCheckResponse,
)
from ditto.api_server.dependencies import get_chain_client, get_price_oracle
from ditto.api_server.pricing import (
    MalformedPriceError,
    PriceOracle,
)
from ditto.chain import ChainError

if TYPE_CHECKING:
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["upload"])

# `/upload/check` failure codes live in the 1xxx agent-side range per
# CODE-REVIEW-CHECKLIST.md. New codes added here go in 110x.
ERROR_CODE_BAD_SIGNATURE = 1100
ERROR_CODE_HOTKEY_NOT_REGISTERED = 1101
ERROR_CODE_TARBALL_TOO_LARGE = 1102

# Hard cap shared with /upload/agent (next PR). Tarballs above this
# size are rejected pre-payment.
MAX_TARBALL_SIZE_BYTES = 2 * 1024 * 1024

ChainDep = Annotated["ChainClient", Depends(get_chain_client)]
OracleDep = Annotated[PriceOracle, Depends(get_price_oracle)]


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
