from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import zstandard

from ditto.api_server.screening_traces import (
    SCREENING_TRACE_SCHEMA,
    encode_screening_trace,
    screening_trace_key,
)


def test_screening_trace_key_nests_under_traces_v1() -> None:
    key = screening_trace_key(
        kind="kaniko",
        provider="targon",
        build_id=UUID("00c74c38-d247-45fa-8ff7-9d2acb5e9b66"),
        uid="wrk-vj9fbj8cmayk",
        now=datetime(2026, 8, 26, 6, 20, tzinfo=UTC),
    )
    assert key.startswith("traces/v1/lane=screening/kind=kaniko/dt=2026-08-26/hour=06/")
    assert key.endswith(".jsonl.zst")


def test_encode_screening_trace_is_zstd_jsonl() -> None:
    body = encode_screening_trace(
        {"schema": SCREENING_TRACE_SCHEMA, "log_tail": "kaniko: rustc oom"}
    )
    line = zstandard.ZstdDecompressor().decompress(body).decode()
    assert SCREENING_TRACE_SCHEMA in line
    assert line.endswith("\n")
