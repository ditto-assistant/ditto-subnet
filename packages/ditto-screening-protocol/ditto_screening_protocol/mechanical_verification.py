"""Canonical V13 mechanical evidence commitment shared by worker and Platform.

The profile digest marks receipts validated by Platform against its own
committed artifact and verified image rows. It is not a V13 CLEAR verdict.
"""

from __future__ import annotations

import hashlib
import json

MECHANICAL_PROFILE = "v13-mechanical-receipt-v1"
MECHANICAL_PROFILE_SHA256 = hashlib.sha256(MECHANICAL_PROFILE.encode()).hexdigest()


def mechanical_evidence_sha256(
    *, check_code: str, artifact_sha256: str, image_sha256: str | None = None
) -> str:
    if check_code not in {"archive_sha", "build_image_digest"}:
        raise ValueError("unsupported mechanical verification check")
    if len(artifact_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in artifact_sha256
    ):
        raise ValueError("invalid artifact SHA-256")
    if (check_code == "build_image_digest") != (image_sha256 is not None):
        raise ValueError("image SHA-256 binding does not match check")
    if image_sha256 is not None and (
        len(image_sha256) != 64
        or any(character not in "0123456789abcdef" for character in image_sha256)
    ):
        raise ValueError("invalid image SHA-256")
    record = {
        "profile": MECHANICAL_PROFILE,
        "check_code": check_code,
        "artifact_sha256": artifact_sha256,
        "image_sha256": image_sha256,
    }
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
