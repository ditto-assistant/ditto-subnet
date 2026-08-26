"""Upload screening replica log tails to the private traces archive."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import zstandard

SCREENING_TRACE_SCHEMA = "ditto.screening.trace.v1"

PutTrace = Callable[[str, bytes, str], Awaitable[str]]


def screening_trace_key(
    *,
    kind: str,
    provider: str,
    build_id: UUID,
    uid: str,
    now: datetime | None = None,
) -> str:
    captured = now or datetime.now(UTC)
    safe_uid = uid.replace("/", "-").replace(":", "-")[-32:]
    return (
        f"traces/v1/lane=screening/kind={kind}/"
        f"dt={captured:%Y-%m-%d}/hour={captured:%H}/"
        f"{provider}-{build_id.hex}-{safe_uid}.jsonl.zst"
    )


def encode_screening_trace(record: dict[str, Any]) -> bytes:
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    return zstandard.ZstdCompressor(level=3).compress(line.encode())
