"""Materialize one protected, plaintext private v2 payload bundle offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ditto.api_server.coding_private_v2_payload import (
    PrivateV2PayloadError,
    build_private_v2_payload,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--groups-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        authority = build_private_v2_payload(
            catalog_directory=args.catalog,
            groups_root=args.groups_root,
            output=args.output,
        )
    except PrivateV2PayloadError as error:
        print(f"private v2 payload build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(authority, sort_keys=True), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
