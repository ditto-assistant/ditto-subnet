"""Audited operator setting for the public transcript mirror."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class TranscriptMirrorSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    enabled: bool
    reason: str
    actor: str
    created_at: datetime | None


class AdminTranscriptMirrorSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: TranscriptMirrorSettingsRevision
    history: list[TranscriptMirrorSettingsRevision]


class AdminTranscriptMirrorSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    enabled: bool
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


def transcript_mirror_confirmation(enabled: bool) -> str:
    return "ENABLE TRANSCRIPT MIRROR" if enabled else "DISABLE TRANSCRIPT MIRROR"
