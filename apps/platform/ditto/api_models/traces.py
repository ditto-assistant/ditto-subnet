"""Private operator models for the inference trace archive (Hippius S3)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TraceObject(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    key: str
    size: int
    last_modified: str
    etag: str


class TraceObjectList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    bucket: str
    prefix: str
    objects: list[TraceObject]
    continuation_token: str | None
    """Pass back verbatim to read the next page; null means complete."""


class TraceDownloadURLRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    key: str = Field(min_length=1, max_length=1024)
    expires_in: int = Field(default=300, ge=60, le=3600)


class TraceDownloadURL(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    bucket: str
    key: str
    url: str
    expires_in: int


class TracePeekRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    key: str = Field(min_length=1, max_length=1024)
    max_records: int = Field(default=5, ge=1, le=50)
    offset_records: int = Field(default=0, ge=0, le=100_000)
    include_bodies: bool = False
    """False returns per-record summaries only; true attaches each full
    record (request/response bodies included), which is miner-sensitive."""


class TraceRecordSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    index: int
    recorded_at: str | None = None
    event: str | None = None
    lane: str | None = None
    kind: str | None = None
    run_id: str | None = None
    case_id: str | None = None
    grant_id: str | None = None
    nonce: str | None = None
    agent_id: str | None = None
    validator_hotkey: str | None = None
    bench_version: int | None = None
    status: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    provider: str | None = None
    latency_ms: int | None = None
    body_bytes: int | None = None
    record: dict[str, Any] | None = None
    """The full record when include_bodies was requested and the record fit."""
    record_omitted: Literal["too_large"] | None = None


class TracePeekResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    bucket: str
    key: str
    records: list[TraceRecordSummary]
    records_scanned: int
    scan_complete: bool
    """False when the bounded scan ended (size caps) before the file did."""
