"""Compile verified private v2 group authorities into sealed catalog leaves."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ditto.api_server.coding_private_catalog_v2_compile import (
    PrivateCatalogV2CompileError,
    compile_private_catalog_v2,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-authority", type=Path, required=True)
    parser.add_argument("--groups-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        authority = compile_private_catalog_v2(
            release_authority=args.release_authority,
            groups_root=args.groups_root,
            output=args.output,
        )
    except PrivateCatalogV2CompileError as error:
        print(f"private v2 catalog compilation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(authority, sort_keys=True), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
