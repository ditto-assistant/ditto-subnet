"""Audited per-node concurrency controls for federated screener workers."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScreenerNodeChannelSettings(BaseModel):
    """Hard Platform admission limits for one enrolled screener node.

    Build and runtime work share the same disposable-VM pool. Their individual
    limits allow operators to reserve or disable a lane, while ``sandbox_slots``
    prevents both lanes from consuming twice the physical host capacity.
    Source review is CPU-light and has its own independent limit.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    screening_concurrency: Annotated[int, Field(ge=0, le=32)] = 0
    sandbox_slots: Annotated[int, Field(ge=0, le=16)] = 0
    build_concurrency: Annotated[int, Field(ge=0, le=16)] = 0
    runtime_concurrency: Annotated[int, Field(ge=0, le=16)] = 0
    source_review_concurrency: Annotated[int, Field(ge=0, le=32)] = 0

    @model_validator(mode="after")
    def validate_shared_sandbox(self) -> ScreenerNodeChannelSettings:
        if self.build_concurrency > self.sandbox_slots:
            raise ValueError("build concurrency cannot exceed sandbox slots")
        if self.runtime_concurrency > self.sandbox_slots:
            raise ValueError("runtime concurrency cannot exceed sandbox slots")
        return self


class ScreenerNodeChannelSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: str
    node_id: str
    revision: int
    parent_revision: int
    settings: ScreenerNodeChannelSettings
    reason: str
    actor: str
    created_at: datetime | None


class EffectiveScreenerNodeChannelSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: str
    node_id: str
    revision: int
    settings: ScreenerNodeChannelSettings


class ScreenerNodeChannelSettingsControl(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: ScreenerNodeChannelSettingsRevision
    history: list[ScreenerNodeChannelSettingsRevision]
    usage: ScreenerNodeChannelUsage | None = None


class ScreenerNodeChannelUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    screening_active: Annotated[int, Field(ge=0)] = 0
    sandbox_active: Annotated[int, Field(ge=0)] = 0
    build_active: Annotated[int, Field(ge=0)] = 0
    runtime_active: Annotated[int, Field(ge=0)] = 0
    source_review_active: Annotated[int, Field(ge=0)] = 0


class ScreenerNodeChannelSettingsWriteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: ScreenerNodeChannelSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class ScreenerNodeAdminStatusWriteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod"
    expected_status: Literal["active", "draining", "quarantined"]
    status: Literal["active", "draining", "quarantined"]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


def node_channel_settings_confirmation(
    node_id: str, settings: ScreenerNodeChannelSettings
) -> str:
    return (
        f"APPLY SCREENER NODE {node_id} "
        f"SCREENING={settings.screening_concurrency} "
        f"SANDBOX={settings.sandbox_slots} "
        f"BUILD={settings.build_concurrency} "
        f"RUNTIME={settings.runtime_concurrency} "
        f"SOURCE_REVIEW={settings.source_review_concurrency}"
    )


def node_status_confirmation(node_id: str, status: str) -> str:
    return f"SET SCREENER NODE {node_id} STATUS={status.upper()}"
