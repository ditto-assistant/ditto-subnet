"""Read a scorer-owned, release-bound description of injected V13 sandbox env.

This is evidence about host injection only. A miner image may declare ENV and
source may supply defaults; the reviewer must still trace both before CLEAR.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def validate_runtime_evidence(
    payload: object, *, expected_revision: str
) -> dict[str, object]:
    """Reject a stale, environment-asserted, or malformed scorer claim."""
    if not _SHA.fullmatch(expected_revision) or not isinstance(payload, Mapping):
        raise ValueError("scorer release identity is unavailable")
    if (
        payload.get("source_revision") != expected_revision
        or payload.get("source_revision_origin") != "binary"
        or payload.get("source_revision_mismatch") is not False
    ):
        raise ValueError("scorer release identity does not match")
    raw = payload.get("scored_runtime_env")
    if not isinstance(raw, Mapping):
        raise ValueError("scorer runtime environment evidence is unavailable")
    keys = raw.get("injected_keys")
    if (
        raw.get("bench_version") != 13
        or isinstance(raw.get("bench_version"), bool)
        or raw.get("scope") != "scorer-injected-env-only"
        or raw.get("source_revision") != expected_revision
        or not isinstance(keys, list)
        or not keys
        or any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in keys)
        or keys != sorted(set(keys))
    ):
        raise ValueError("scorer runtime environment evidence is malformed")
    material = (
        "scored-runtime-env-v1\n13\n" + expected_revision + "\n" + "\n".join(keys)
    )
    digest = hashlib.sha256(material.encode()).hexdigest()
    if raw.get("sha256") != digest:
        raise ValueError("scorer runtime environment digest does not match")
    return {
        "bench_version": 13,
        "scope": "scorer-injected-env-only",
        "source_revision": expected_revision,
        "injected_keys": keys,
        "sha256": digest,
        "limits": (
            "Only scorer-injected variables are covered. Check image ENV, code "
            "defaults, runtime writes, and all I1-I7 paths independently."
        ),
    }


async def fetch_runtime_evidence(
    capabilities_url: str,
    *,
    expected_revision: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    parsed = urlsplit(capabilities_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1/capabilities"
    ):
        raise ValueError("scorer capabilities URL must be a fixed HTTPS endpoint")
    async with httpx.AsyncClient(
        transport=transport, timeout=5, follow_redirects=False
    ) as client:
        response = await client.get(capabilities_url)
        response.raise_for_status()
        if len(response.content) > 16_384:
            raise ValueError("scorer runtime evidence exceeds size bound")
        return validate_runtime_evidence(
            response.json(), expected_revision=expected_revision
        )
