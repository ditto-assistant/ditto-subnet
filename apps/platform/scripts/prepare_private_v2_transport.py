"""Envelope-encrypt a verified private v2 payload without provider I/O."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ditto.api_server.coding_private_v2_transport import (
    PRIVATE_V2_TRANSPORT_CONFIRMATION,
    PrivateV2TransportError,
    prepare_private_v2_transport,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--wrapping-public-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.confirm != PRIVATE_V2_TRANSPORT_CONFIRMATION:
        parser.error("invalid --confirm value")
    try:
        manifest = prepare_private_v2_transport(
            payload_directory=args.payload,
            wrapping_public_key=args.wrapping_public_key,
            output=args.output,
        )
    except PrivateV2TransportError as error:
        print(f"private v2 transport preparation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, sort_keys=True), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
