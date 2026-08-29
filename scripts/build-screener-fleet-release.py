#!/usr/bin/env python3
"""Render the authenticated release descriptor consumed by fleet hosts."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

IMAGE_RE = re.compile(
    r"^us-central1-docker\.pkg\.dev/ditto-app-dev/ditto-public-builders/"
    r"submission-builder@sha256:[0-9a-f]{64}$"
)
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--update-protocol", default="1")
    parser.add_argument("--submission-builder-image", required=True)
    args = parser.parse_args()

    if not VERSION_RE.fullmatch(args.version):
        raise ValueError("version must be an unprefixed semantic version")
    if not REVISION_RE.fullmatch(args.revision):
        raise ValueError("revision must be a full lowercase Git SHA")
    if not args.update_protocol.isdigit() or int(args.update_protocol) < 1:
        raise ValueError("update protocol must be a positive integer")
    if not IMAGE_RE.fullmatch(args.submission_builder_image):
        raise ValueError("submission builder must be an immutable image reference")

    values = {
        "FLEET_FORMAT_VERSION": "1",
        "FLEET_VERSION": args.version,
        "FLEET_REVISION": args.revision,
        "FLEET_UPDATE_PROTOCOL": args.update_protocol,
        "SUBMISSION_BUILDER_IMAGE": args.submission_builder_image,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items())
    )


if __name__ == "__main__":
    main()
