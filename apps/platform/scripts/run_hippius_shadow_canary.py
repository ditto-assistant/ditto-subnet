"""Run one confirmation-gated synthetic Hippius Coding canary."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ditto.api_server.coding_hippius_canary import (
    HIPPIUS_SHADOW_CANARY_CONFIRMATION,
)
from ditto.api_server.coding_hippius_canary_operator import (
    HippiusCanaryOperatorError,
    run_hippius_canary_operator_from_env,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        required=True,
        help=f"must be exactly: {HIPPIUS_SHADOW_CANARY_CONFIRMATION}",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.confirm != HIPPIUS_SHADOW_CANARY_CONFIRMATION:
        parser.error(f"--confirm must be exactly: {HIPPIUS_SHADOW_CANARY_CONFIRMATION}")
    try:
        canary_sha256, payload_sha256 = asyncio.run(
            run_hippius_canary_operator_from_env(
                repository_root=_REPOSITORY_ROOT,
                confirmation=args.confirm,
                output=args.output,
            )
        )
    except HippiusCanaryOperatorError as error:
        print(f"Hippius Coding shadow canary failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Hippius Coding shadow canary failed: unexpected internal error",
            file=sys.stderr,
        )
        return 2
    print(
        "Hippius Coding shadow canary complete; ready=true; "
        f"canary_run_sha256={canary_sha256}; "
        f"receipt_payload_sha256={payload_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
