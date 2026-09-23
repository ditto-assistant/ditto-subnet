"""Digest-only mechanical verification receipts for the active v13 attempt.

These hashes attest only that the trusted gate observed the named mechanical
step. They do not attest policy completion or private metamorphic verification.
"""

from __future__ import annotations

import hashlib
import json


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
        "profile": "v13-mechanical-receipt-v1",
        "check_code": check_code,
        "artifact_sha256": artifact_sha256,
        "image_sha256": image_sha256,
    }
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
