#!/usr/bin/env python3
"""Render the canonical release manifest for the dedicated coding scorer."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--locked-policy-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repository, separator, digest = args.image_reference.partition("@")
    if (
        not repository
        or separator != "@"
        or not digest.startswith("sha256:")
        or not SHA256.fullmatch(digest[7:])
    ):
        raise SystemExit("image reference must be repository@sha256:<lowercase digest>")
    if not SOURCE.fullmatch(args.source_revision) or not SHA256.fullmatch(
        args.locked_policy_sha256
    ):
        raise SystemExit("release manifest digest input is invalid")
    value = {
        "image_digest": digest,
        "image_reference": args.image_reference,
        "locked_policy_sha256": args.locked_policy_sha256,
        "platform": "linux/amd64",
        "schema": "dittobench-coding-executor-scorer-release-v1",
        "scorer_contract": "1",
        "source_revision": args.source_revision,
    }
    args.output.write_bytes(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
