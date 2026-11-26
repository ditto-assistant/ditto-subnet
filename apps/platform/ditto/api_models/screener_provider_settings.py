"""Audited provider routing for independent screening lanes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ScreenerCapacityProvider = Literal["hetzner", "aws", "targon", "gcp"]

# Providers whose enrolled node can be the audited primary in front of GCE
# overflow. Hetzner is the fixed-cost host; AWS is the on-demand EC2 host.
PRIMARY_SCREENER_PROVIDERS: tuple[ScreenerCapacityProvider, ...] = ("hetzner", "aws")


class ScreenerProviderSettings(BaseModel):
    """Ordered provider lists for build, runtime, and source-review lanes."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    # An unconfigured installation has no enrolled primary node. The audited
    # production revision selects the primary with GCE fallback explicitly.
    runtime_provider_priority: tuple[ScreenerCapacityProvider, ...] = ("gcp",)
    source_review_provider_priority: tuple[ScreenerCapacityProvider, ...] = ("gcp",)
    build_provider_priority: tuple[ScreenerCapacityProvider, ...] = ("gcp",)
    gce_overflow_enabled: bool = False
    primary_node_id: Annotated[
        str | None, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    ] = None
    gce_overflow_backlog_multiplier: Annotated[int, Field(ge=2, le=20)] = 3
    gce_overflow_min_backlog: Annotated[int, Field(ge=1, le=1000)] = 12
    gce_overflow_max_instances: Annotated[int, Field(ge=0, le=32)] = 6

    @model_validator(mode="after")
    def validate_provider_lists(self) -> ScreenerProviderSettings:
        for field, providers in (
            ("runtime_provider_priority", self.runtime_provider_priority),
            (
                "source_review_provider_priority",
                self.source_review_provider_priority,
            ),
            ("build_provider_priority", self.build_provider_priority),
        ):
            if not providers:
                raise ValueError(f"{field} must not be empty")
            if len(providers) != len(set(providers)):
                raise ValueError(f"{field} must not contain duplicates")
            if "gcp" not in providers:
                raise ValueError(f"{field} must retain the GCP safety fallback")
        if self.gce_overflow_enabled:
            if self.primary_node_id is None:
                raise ValueError("GCE overflow requires a primary node")
            for field, providers in (
                ("runtime_provider_priority", self.runtime_provider_priority),
                (
                    "source_review_provider_priority",
                    self.source_review_provider_priority,
                ),
                ("build_provider_priority", self.build_provider_priority),
            ):
                if providers[0] not in PRIMARY_SCREENER_PROVIDERS:
                    raise ValueError(
                        f"{field} must be primary-first (Hetzner or AWS) with "
                        "GCP available when overflow is enabled"
                    )
            if self.primary_provider() is None:
                raise ValueError(
                    "every lane must name the same primary provider first "
                    "when overflow is enabled"
                )
        return self

    def all_lanes_gcp_only(self) -> bool:
        return (
            self.runtime_provider_priority == ("gcp",)
            and self.source_review_provider_priority == ("gcp",)
            and self.build_provider_priority == ("gcp",)
        )

    def primary_provider(self) -> ScreenerCapacityProvider | None:
        """The primary provider every lane names first, if they agree on one."""
        first = {
            self.runtime_provider_priority[0],
            self.source_review_provider_priority[0],
            self.build_provider_priority[0],
        }
        if len(first) != 1:
            return None
        (provider,) = first
        return provider if provider in PRIMARY_SCREENER_PROVIDERS else None


class ScreenerProviderSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: str
    revision: int
    parent_revision: int
    settings: ScreenerProviderSettings
    reason: str
    actor: str
    created_at: datetime | None


class EffectiveScreenerProviderSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: str
    revision: int
    settings: ScreenerProviderSettings


class ScreenerProviderSettingsControl(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: ScreenerProviderSettingsRevision
    history: list[ScreenerProviderSettingsRevision]


class ScreenerProviderSettingsWriteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: ScreenerProviderSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


def provider_settings_confirmation(settings: ScreenerProviderSettings) -> str:
    runtime = ">".join(settings.runtime_provider_priority)
    source_review = ">".join(settings.source_review_provider_priority)
    builds = ">".join(settings.build_provider_priority)
    overflow = (
        f"ENABLED:{settings.primary_node_id}:"
        f"{settings.gce_overflow_backlog_multiplier}X:"
        f"MIN={settings.gce_overflow_min_backlog}:"
        f"MAX={settings.gce_overflow_max_instances}"
        if settings.gce_overflow_enabled
        else "DISABLED"
    )
    return (
        f"APPLY SCREENER PROVIDERS BUILDS={builds} RUNTIME={runtime} "
        f"SOURCE_REVIEW={source_review} GCE_OVERFLOW={overflow}"
    )
