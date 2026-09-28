"""Verify replay image bytes and their actual Docker config identity.

This is one prerequisite for a trusted V13 replay, not a build receipt or a
policy decision. The caller keeps the download in a private worker-owned
directory and must load the normalized archive into a fresh isolated runtime.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from ditto_screener.gate import BuildGate

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_IMAGE_BYTES = 8 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024


class ReplayImageUnavailable(ValueError):
    """The image cannot support a trusted runtime observation."""


@dataclass(frozen=True)
class VerifiedReplayArchive:
    archive_sha256: str
    archive_size_bytes: int
    image_id: str
    portable_path: Path


def verify_replay_image_archive(
    *,
    archive_path: Path,
    portable_path: Path,
    expected_sha256: str,
    expected_size_bytes: int,
    expected_image_id: str,
    deadline: float,
) -> VerifiedReplayArchive:
    """Hash the exact tar and verify its internal config/layer commitments.

    The expected values must come from the current Platform replay lease. A
    worker-claimed image ID is accepted only if the tar's config bytes yield it.
    Failure removes any partially normalized output and leaves the check
    incomplete. A private, non-writable-by-miners directory is mandatory.
    """
    if (
        _SHA256.fullmatch(expected_sha256) is None
        or _IMAGE_ID.fullmatch(expected_image_id) is None
        or not 0 < expected_size_bytes <= _MAX_IMAGE_BYTES
        or deadline <= time.monotonic()
        or archive_path == portable_path
        or portable_path.exists()
        or portable_path.is_symlink()
    ):
        raise ReplayImageUnavailable("replay image commitment invalid")
    descriptor: int | None = None
    success = False
    try:
        descriptor = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected_size_bytes
        ):
            raise ReplayImageUnavailable("replay image size mismatch")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _CHUNK):
            if time.monotonic() >= deadline:
                raise ReplayImageUnavailable("replay image verification expired")
            digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ReplayImageUnavailable("replay image digest mismatch")
        # The existing image normalizer validates the single-image manifest,
        # config digest, every layer digest, and bounded portable output.
        portable = BuildGate._portable_image_archive(
            str(archive_path), str(portable_path), deadline=deadline
        )
        if portable.image_id != expected_image_id:
            raise ReplayImageUnavailable("replay image config identity mismatch")
        success = True
        return VerifiedReplayArchive(
            archive_sha256=expected_sha256,
            archive_size_bytes=expected_size_bytes,
            image_id=portable.image_id,
            portable_path=portable_path,
        )
    except ReplayImageUnavailable:
        raise
    except Exception:
        raise ReplayImageUnavailable("replay image verification unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # Only the successful call keeps a portable archive. A caller must
        # delete it after the isolated runtime has loaded and inspected it.
        if not success:
            portable_path.unlink(missing_ok=True)
