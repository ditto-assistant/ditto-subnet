"""Content-addressed, text-free observations of v13 runtime probes.

These are execution receipts only. A 2xx or model call is not a pass for tool
selection, memory correctness, user isolation, or any private policy check.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

RUNTIME_CHECKS = frozenset(
    {
        "health",
        "ordinary_model_run",
        "tool_selection_run",
        "seed_memory_run",
        "two_user_isolation",
    }
)


def runtime_evidence_sha256(
    *,
    check_code: str,
    artifact_sha256: str,
    image_id: str,
    request_sha256s: Sequence[str],
    response_sha256s: Sequence[str],
    broker_calls: int,
) -> str:
    """Hash only bounded observation digests; never persist probe contents.

    The image ID is the immutable Docker config digest of the container that
    answered. Platform also binds the receipt to the exact active attempt,
    artifact SHA and policy version, and labels it ``recorded_unverified``.
    """
    if check_code not in RUNTIME_CHECKS:
        raise ValueError("unsupported runtime verification check")
    if len(artifact_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in artifact_sha256
    ):
        raise ValueError("invalid artifact SHA-256")
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError("invalid image ID")
    if any(c not in "0123456789abcdef" for c in image_id[7:]):
        raise ValueError("invalid image ID")
    if broker_calls < 0 or len(request_sha256s) != len(response_sha256s):
        raise ValueError("invalid runtime observation")
    for digest in (*request_sha256s, *response_sha256s):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid runtime observation digest")
    record: Mapping[str, object] = {
        "profile": "v13-runtime-observation-v1",
        "check_code": check_code,
        "artifact_sha256": artifact_sha256,
        "image_id": image_id,
        "request_sha256s": list(request_sha256s),
        "response_sha256s": list(response_sha256s),
        "broker_calls": broker_calls,
    }
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
