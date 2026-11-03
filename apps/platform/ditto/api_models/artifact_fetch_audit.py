"""Operator view of who was handed an artifact's source or screened image."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArtifactFetchAuditRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fetched_at: datetime
    agent_id: UUID
    artifact_sha256: str | None
    endpoint: str
    requester_kind: str
    requester_id: str | None
    requester_instance_id: str | None
    lease_id: UUID | None
    bench_version: int | None
    # Rows written before the route recorded it served source on every
    # source-bearing route, so a missing flag reads as ``True`` there.
    served_source: bool
    served_screened_image: bool | None
    # Truncated hash for correlating rows; the peer address is never returned.
    source_ip_hash: str | None
    forwarded_claim_present: bool


class ArtifactFetchRequesterSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    requester_kind: str
    requester_id: str | None
    fetch_count: int = Field(ge=0)
    source_fetch_count: int = Field(ge=0)
    first_fetched_at: datetime
    last_fetched_at: datetime


class ArtifactFetchAudit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_ids: list[UUID]
    artifact_sha256: str | None
    before: datetime
    total: int = Field(ge=0)
    truncated: bool
    requesters: list[ArtifactFetchRequesterSummary]
    rows: list[ArtifactFetchAuditRow]
    absence_proves_no_fetch: Literal[False] = False
    limitations: str = (
        "The fetch audit is fail-open: a dropped audit write leaves no row, so "
        "absence never proves an artifact was not served. A row records a "
        "served presigned URL, not a completed download. Screener node jobs "
        "share fleet identities; requester_id names the node or provider."
    )
