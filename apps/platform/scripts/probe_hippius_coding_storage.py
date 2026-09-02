"""Probe the synthetic Hippius Coding data plane and write a redacted receipt."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_PROBE_CONFIRMATION,
    AiobotoHippiusProbeTransport,
    HippiusProbeError,
    parse_hippius_probe_config,
    resolve_repository_source_sha,
    run_hippius_capability_probe,
    write_hippius_probe_receipt,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


async def _run(*, output: Path) -> tuple[bool, str]:
    config = parse_hippius_probe_config()
    source_sha = resolve_repository_source_sha(_REPOSITORY_ROOT)
    async with AiobotoHippiusProbeTransport(config) as transport:
        receipt = await run_hippius_capability_probe(
            config=config,
            transport=transport,
            source_sha=source_sha,
        )
    payload_sha256 = write_hippius_probe_receipt(receipt=receipt, output=output)
    return receipt.ready, payload_sha256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        required=True,
        help=f"must be exactly: {HIPPIUS_PROBE_CONFIRMATION}",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.confirm != HIPPIUS_PROBE_CONFIRMATION:
        parser.error(f"--confirm must be exactly: {HIPPIUS_PROBE_CONFIRMATION}")
    try:
        ready, payload_sha256 = asyncio.run(_run(output=args.output))
    except HippiusProbeError as error:
        print(f"Hippius Coding capability probe failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Hippius Coding capability probe failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(
        "Hippius Coding capability probe complete; "
        f"ready={str(ready).lower()}; receipt_payload_sha256={payload_sha256}"
    )
    return 0 if ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
