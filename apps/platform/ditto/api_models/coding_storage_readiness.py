"""Redacted operator visibility for dedicated Coding Bench storage."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CodingStorageAuthorityReadiness(BaseModel):
    """One authority's exact-object observation without storage coordinates."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["ready", "missing", "drifted", "unavailable"]
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=1)]
    exact_object_verified: bool


class AdminCodingStorageReadinessResponse(BaseModel):
    """Admin-only, secret-free readiness snapshot for the Coding data plane."""

    model_config = ConfigDict(extra="ignore")

    schema_name: Literal["dittobench-coding-storage-readiness-v1"] = Field(
        alias="schema"
    )
    environment: Literal["dev", "prod"]
    source_sha: str
    checked_at: datetime
    ready: bool
    private_input: CodingStorageAuthorityReadiness
    sealed_evidence: CodingStorageAuthorityReadiness
    authorities_distinct: Literal[True]
    read_only: Literal[True]
    weight_eligible: Literal[False]
