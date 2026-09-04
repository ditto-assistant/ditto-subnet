"""Create a single-record shadow-only private v2 canary authority."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ditto.api_server.coding_private_v2_shadow_plan import (
    PrivateV2ShadowPlanError,
    build_private_v2_shadow_canary,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--catalog-index", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        canary = build_private_v2_shadow_canary(
            registration_authority=args.registration,
            catalog_directory=args.catalog,
            catalog_index=args.catalog_index,
            output=args.output,
        )
    except PrivateV2ShadowPlanError as error:
        print(f"private v2 shadow canary preparation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(canary, sort_keys=True), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
