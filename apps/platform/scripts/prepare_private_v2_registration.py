"""Create a shadow-only append-only-ready private v2 registration authority."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ditto.api_server.coding_private_v2_shadow_plan import (
    PrivateV2ShadowPlanError,
    build_private_v2_registration_authority,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--transport", type=Path, required=True)
    parser.add_argument("--publication-receipt-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        authority = build_private_v2_registration_authority(
            catalog_directory=args.catalog,
            payload_directory=args.payload,
            transport_directory=args.transport,
            publication_receipt_sha256=args.publication_receipt_sha256,
            output=args.output,
        )
    except PrivateV2ShadowPlanError as error:
        print(f"private v2 registration preparation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(authority, sort_keys=True), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
