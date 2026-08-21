"""Resolve the Kaniko runner image to a pullable Artifact Registry digest."""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_TAG = re.compile(r"^sha-[0-9a-f]{40}$")


def is_digest_pinned_image(image: str) -> bool:
    """True when the reference already names an immutable config digest."""
    name = image.rsplit("/", 1)[-1]
    if "@" not in name:
        return False
    return _DIGEST.fullmatch(name.rsplit("@", 1)[-1]) is not None


def resolve_submission_builder_image(image: str) -> str:
    """Return ``repo@sha256:...`` for the requested tag, never a stale latest.

    Platform tags Cloud Run / Targon with ``:sha-{running_commit}``. Falling
    back to the newest published digest when that tag is missing launches an
    older Kaniko helper that does not post ``image_id``. Bind then fail-closes.
    Commit tags must match exactly. Floating tags may still use latest.

    The helper at that tag must parse Kaniko/go-containerregistry docker-save
    config members named ``sha256:<hex>``. Classic ``<hex>.json`` names are
    not what ``--tar-path`` writes.
    """
    requested = image.strip()
    if not requested:
        return image
    if is_digest_pinned_image(requested):
        return requested
    repository, _sep, tag = requested.rpartition(":")
    if not repository or not tag or "/" not in repository:
        return image
    digest = _describe_digest(f"{repository}:{tag}")
    if digest is None and not _COMMIT_TAG.fullmatch(tag):
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
