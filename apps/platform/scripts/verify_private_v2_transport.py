"""Verify a prepared private v2 transport without provider I/O."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ditto.api_server.coding_private_v2_transport import verify_private_v2_transport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transport", type=Path)
    args = parser.parse_args(argv)
    print(
        json.dumps(verify_private_v2_transport(args.transport), sort_keys=True),
        end="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
