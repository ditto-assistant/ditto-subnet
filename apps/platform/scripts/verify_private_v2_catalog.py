"""Verify an offline private v2 catalog compilation directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ditto.api_server.coding_private_catalog_v2_compile import (
    PrivateCatalogV2CompileError,
    verify_private_catalog_v2,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    args = parser.parse_args(argv)
    try:
        authority = verify_private_catalog_v2(args.catalog)
    except PrivateCatalogV2CompileError as error:
        print(f"private v2 catalog verification failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(authority, sort_keys=True), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
