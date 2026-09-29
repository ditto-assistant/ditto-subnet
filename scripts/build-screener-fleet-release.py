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


def resolve_builder_image(primary: str, fallback_file: Path | None) -> tuple[str, str]:
    """Keep protocol 1 compatible without gating delivery on the unused job."""
    if IMAGE_RE.fullmatch(primary):
        return primary, "job"
    if fallback_file is not None:
        try:
            fallback = fallback_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise ValueError(
                "submission builder fallback is unavailable or invalid"
            ) from error
        if IMAGE_RE.fullmatch(fallback):
            return fallback, "fallback"
    raise ValueError("submission builder must be an immutable image reference")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--update-protocol", default="1")
    parser.add_argument("--submission-builder-image", default="")
    parser.add_argument("--submission-builder-fallback-file", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    if not VERSION_RE.fullmatch(args.version):
        raise ValueError("version must be an unprefixed semantic version")
    if not REVISION_RE.fullmatch(args.revision):
        raise ValueError("revision must be a full lowercase Git SHA")
    if not args.update_protocol.isdigit() or int(args.update_protocol) < 1:
        raise ValueError("update protocol must be a positive integer")
    builder_image, source = resolve_builder_image(
        args.submission_builder_image, args.submission_builder_fallback_file
    )
    if source == "fallback":
        print(
            "::warning::Using committed submission-builder fallback from "
            f"{args.submission_builder_fallback_file}"
        )

    values = {
        "FLEET_FORMAT_VERSION": "1",
        "FLEET_VERSION": args.version,
        "FLEET_REVISION": args.revision,
        "FLEET_UPDATE_PROTOCOL": args.update_protocol,
        "SUBMISSION_BUILDER_IMAGE": builder_image,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items())
    )
    if args.github_output is not None:
        # Both values are validated single-line strings. Report the actual
        # selection so workflow verification cannot diverge from the manifest.
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"builder_source={source}\nbuilder_image={builder_image}\n")


if __name__ == "__main__":
    main()
