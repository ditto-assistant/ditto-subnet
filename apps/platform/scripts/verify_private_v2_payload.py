"""Verify a protected private v2 payload bundle offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ditto.api_server.coding_private_v2_payload import (
    PrivateV2PayloadError,
    verify_private_v2_payload,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    args = parser.parse_args(argv)
    try:
        authority = verify_private_v2_payload(args.payload)
    except PrivateV2PayloadError as error:
        print(f"private v2 payload verification failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(authority, sort_keys=True), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
