"""Read-only, same-finalized-block quotes for the two GM top-up routes."""

from __future__ import annotations

import asyncio
from typing import Annotated

import bittensor as bt
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ditto.api_server.endpoints.admin_quarantine import require_admin

router = APIRouter(prefix="/admin/treasury-quote", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


def _impact_bps(received_rao: int, lost_rao: int) -> int:
    total = received_rao + lost_rao
    return 10_000 * lost_rao // total if total > 0 else 10_000


async def _read_quote(source_alpha_rao: int) -> dict:
    async with asyncio.timeout(15):
        async with bt.AsyncSubtensor(network="finney") as chain:
            block_hash = await chain.substrate.get_chain_finalised_head()
            block = await chain.substrate.get_block_number(block_hash)
            ditto = await chain.subnet(118, block_hash=block_hash)
            gm = await chain.subnet(28, block_hash=block_hash)
            if ditto is None or gm is None:
                raise ValueError("missing finalized subnet pool")
            tao, ditto_slippage = ditto.alpha_to_tao_with_slippage(
                bt.Balance.from_rao(source_alpha_rao).set_unit(118)
            )
            gm_alpha, gm_slippage = gm.tao_to_alpha_with_slippage(tao)
            return {
                "block": block,
                "block_hash": block_hash,
                "source_alpha_rao": source_alpha_rao,
                "tao_path": {
                    "deposit_asset": "TAO",
                    "amount_rao": tao.rao,
                    "price_impact_bps": _impact_bps(tao.rao, ditto_slippage.rao),
                },
                "gm_alpha_path": {
                    "deposit_asset": "SN28_ALPHA",
                    "amount_rao": gm_alpha.rao,
                    "price_impact_bps": _impact_bps(gm_alpha.rao, gm_slippage.rao),
                },
                "gm_credit_usd": None,
                "execution_enabled": False,
                "settlement": (
                    "GM converts the confirmed deposit at its then-current rate"
                ),
            }


@router.get("")
async def get_treasury_quote(
    source_alpha_rao: Annotated[int, Query(gt=0, le=10_000_000_000)],
    _admin: AdminDep,
    response: Response,
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await _read_quote(source_alpha_rao)
    except Exception as error:  # noqa: BLE001 - RPC failures are fail-closed
        raise HTTPException(
            status_code=503, detail="treasury quote unavailable"
        ) from error
