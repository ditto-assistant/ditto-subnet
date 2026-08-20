"""Resolve the Kaniko runner image to a pullable Artifact Registry digest."""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def resolve_submission_builder_image(image: str) -> str:
    """Return ``repo@sha256:...`` when Artifact Registry has a published digest.

    Platform tags Cloud Run / Targon with ``:sha-{running_commit}``. Builder
    images are only published when the release plan includes the orchestrator,
    so skip-ci and platform-only SHAs often have no tag. Cloud Run then sits
    Ready=False for the full provision window. Prefer the requested tag, then
    the newest published digest, then the original reference.
    """
    requested = image.strip()
    if not requested:
        return image
    if "@" in requested.rsplit("/", 1)[-1]:
        pinned = requested.rsplit("@", 1)[-1]
        return requested if _DIGEST.fullmatch(pinned) else image
    repository, _sep, tag = requested.rpartition(":")
    if not repository or not tag or "/" not in repository:
        return image
    digest = _describe_digest(f"{repository}:{tag}")
    if digest is None:
        digest = _latest_digest(repository)
        if digest is not None:
            logger.warning(
                "submission builder tag %s missing; using latest digest %s",
                requested,
                digest,
            )
    if digest is None:
        logger.error("submission builder image %s is unpublished", requested)
        return image
    return f"{repository}@{digest}"


def _describe_digest(image: str) -> str | None:
    output = _gcloud(
        [
            "artifacts",
            "docker",
            "images",
            "describe",
            image,
            "--format=value(image_summary.digest)",
            "--quiet",
        ]
    )
    return output if output is not None and _DIGEST.fullmatch(output) else None


def _latest_digest(repository: str) -> str | None:
    output = _gcloud(
        [
            "artifacts",
            "docker",
            "images",
            "list",
            repository,
            "--include-tags",
            "--sort-by=~CREATE_TIME",
            "--limit=1",
            "--format=value(version)",
            "--quiet",
        ]
    )
    if output is None:
        return None
    match = _DIGEST.search(output)
    return match.group(0) if match else None


def _gcloud(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["gcloud", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()
