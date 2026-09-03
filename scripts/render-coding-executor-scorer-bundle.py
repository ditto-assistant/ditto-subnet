#!/usr/bin/env python3
"""Bind a verified scorer release manifest to one exported OCI archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELEASE_FIELDS = {
    "image_digest",
    "image_reference",
    "locked_policy_sha256",
    "platform",
    "schema",
    "scorer_contract",
    "source_revision",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-manifest", required=True, type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = args.release_manifest.read_bytes()
    release = json.loads(raw)
    if not isinstance(release, dict) or set(release) != RELEASE_FIELDS:
        raise SystemExit("release manifest fields are invalid")
    digest_fields_valid = all(
        isinstance(release.get(field), str)
        and SHA256.fullmatch(release[field].removeprefix("sha256:"))
        for field in ("image_digest", "locked_policy_sha256")
    )
    if (
        release.get("schema") != "dittobench-coding-executor-scorer-release-v1"
        or release.get("platform") != "linux/amd64"
        or release.get("scorer_contract") != "1"
        or not isinstance(release.get("image_reference"), str)
        or "@sha256:" not in release["image_reference"]
        or not digest_fields_valid
        or not SHA256.fullmatch(args.archive_sha256)
        or not args.image_id.startswith("sha256:")
        or not SHA256.fullmatch(args.image_id.removeprefix("sha256:"))
    ):
        raise SystemExit("bundle manifest digest input is invalid")
    value = {
        "archive_sha256": args.archive_sha256,
        "image_digest": release["image_digest"],
        "image_id": args.image_id,
        "image_reference": release["image_reference"],
        "locked_policy_sha256": release["locked_policy_sha256"],
        "platform": "linux/amd64",
        "release_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "schema": "dittobench-coding-executor-scorer-bundle-v1",
        "scorer_contract": "1",
        "source_revision": release["source_revision"],
    }
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    args.output.write_bytes(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
