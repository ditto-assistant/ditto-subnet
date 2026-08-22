"""Canonical known-field JSON for Platform-owned coding contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def coding_canonical_json_bytes(
    projection: dict[str, Any] | list[Any],
    *,
    maximum_bytes: int,
    label: str,
) -> bytes:
    """Encode one already-validated known-field projection."""

    body = (
        (
            json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .encode()
    )
    if len(body) > maximum_bytes:
        raise ValueError(f"canonical {label} exceeds {maximum_bytes} bytes")
    return body


def coding_canonical_sha256(
    projection: dict[str, Any] | list[Any],
    *,
    maximum_bytes: int,
    label: str,
) -> str:
    """Hash one already-validated known-field projection."""

    return hashlib.sha256(
        coding_canonical_json_bytes(
            projection,
            maximum_bytes=maximum_bytes,
            label=label,
        )
    ).hexdigest()
