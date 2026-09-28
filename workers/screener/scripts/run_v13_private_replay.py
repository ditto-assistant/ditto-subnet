"""Run one already-claimed, replay-bound protected V13 group report-only.

Requires an operator-provisioned read-only private bank and independent worker
lease. This script never clears a hold or changes adjudicator settings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

import httpx

from ditto_screener.config import parse_screener_config_from_env
from ditto_screener.enrollment import ensure_node_credentials_from_env
from ditto_screener.platform import PlatformClient
from ditto_screener.signing import load_screener_keypair
from ditto_screener.v13_private_runtime import MAX_PRIVATE_REPLAY_COST_MICROUSD
from ditto_screener.v13_replay_private_run import run_registered_replay_private_group


def _provider_key(path: Path) -> str:
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink() or metadata.st_mode & 0o077:
        raise ValueError("private provider credential is not owner-only")
    key = path.read_text().strip()
    if not key:
        raise ValueError("private provider credential unavailable")
    return key


def _max_cost_microusd(value: str) -> int:
    try:
        amount = Decimal(value) * 1_000_000
        if (
            amount != amount.to_integral_value()
            or not 0 < amount <= MAX_PRIVATE_REPLAY_COST_MICROUSD
        ):
            raise ValueError
        return int(amount)
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError(
            "max cost must be USD between 0.000001 and 20 with microdollar precision"
        ) from None


async def _run(args: argparse.Namespace) -> None:
    await ensure_node_credentials_from_env()
    config = parse_screener_config_from_env()
    keypair = load_screener_keypair(config)
    provider_key = _provider_key(args.provider_key_file)
    async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as http:
        platform = PlatformClient(config, http, keypair=keypair)
        result = await run_registered_replay_private_group(
            replay_id=args.replay_id,
            group_id=args.group_id,
            bank_root=args.bank_root,
            config=config,
            provider_key=provider_key,
            max_cost_microusd=args.max_cost_microusd,
            platform=platform,
            keypair=keypair,
        )
    print(
        json.dumps(
            {
                "replay_id": str(args.replay_id),
                "receipt_sha256": result["receipt_sha256"],
                "status": result["status"],
                "policy_verification_complete": result["policy_verification_complete"],
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-id", type=UUID, required=True)
    parser.add_argument("--group-id", type=UUID, required=True)
    parser.add_argument("--bank-root", type=Path, required=True)
    parser.add_argument("--provider-key-file", type=Path, required=True)
    parser.add_argument(
        "--max-cost-usd",
        type=_max_cost_microusd,
        dest="max_cost_microusd",
        required=True,
    )
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
