"""Prepare a protected two-request authority for the Hippius canary unwrap service."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ditto.api_server.coding_hippius_canary_unwrap import (
    HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION,
    HippiusCanaryUnwrapAuthorityError,
    prepare_hippius_canary_unwrap_authority,
    write_hippius_canary_unwrap_authority,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--publication-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--confirm",
        required=True,
        help=f"must be exactly: {HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION}",
    )
    args = parser.parse_args(argv)
    if args.confirm != HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION:
        parser.error(
            f"--confirm must be exactly: {HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION}"
        )
    try:
        authority = prepare_hippius_canary_unwrap_authority(
            plan_path=args.plan,
            manifest_path=args.manifest,
            publication_receipt_path=args.publication_receipt,
            confirmation=args.confirm,
        )
        authority_sha256 = write_hippius_canary_unwrap_authority(
            authority=authority,
            output=args.output,
        )
    except HippiusCanaryUnwrapAuthorityError as error:
        print(f"Hippius canary unwrap authority failed: {error}", file=sys.stderr)
        return 2
    print(
        f"Hippius canary unwrap authority prepared; authority_sha256={authority_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
