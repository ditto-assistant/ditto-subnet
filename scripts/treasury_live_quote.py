"""Read-only same-block SDK quote; no wallet or transaction methods are used."""

from __future__ import annotations

import argparse
import asyncio
import json

import bittensor as bt


async def quote(alpha_rao: int) -> None:
    async with bt.AsyncSubtensor(network="finney") as chain:
        block_hash = await chain.substrate.get_chain_finalised_head()
        block = await chain.substrate.get_block_number(block_hash)
        ditto = await chain.subnet(118, block_hash=block_hash)
        gm = await chain.subnet(28, block_hash=block_hash)
        if ditto is None or gm is None:
            raise RuntimeError("both subnet pools must be available at one block")
        tao, ditto_slippage = ditto.alpha_to_tao_with_slippage(
            bt.Balance.from_rao(alpha_rao).set_unit(118)
        )
        gm_alpha, gm_slippage = gm.tao_to_alpha_with_slippage(tao)
        print(
            json.dumps(
                {
                    "block": block,
                    "block_hash": block_hash,
                    "source_alpha_rao": alpha_rao,
                    "ditto_tao_reserve_rao": ditto.tao_in.rao,
                    "ditto_alpha_reserve_rao": ditto.alpha_in.rao,
                    "gm_tao_reserve_rao": gm.tao_in.rao,
                    "gm_alpha_reserve_rao": gm.alpha_in.rao,
                    "tao_path_tao_rao": tao.rao,
                    "gm_alpha_path_alpha_rao": gm_alpha.rao,
                    "ditto_slippage_rao": ditto_slippage.rao,
                    "gm_slippage_rao": gm_slippage.rao,
                    "note": (
                        "indicative SDK quote only; GM credits settle "
                        "at confirmed deposit rate"
                    ),
                },
                sort_keys=True,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alpha-rao", type=int, required=True)
    args = parser.parse_args()
    if not 0 < args.alpha_rao <= 10_000_000_000:
        parser.error("read-only probe accepts 1 through 10 DITTO alpha")
    asyncio.run(quote(args.alpha_rao))


if __name__ == "__main__":
    main()
