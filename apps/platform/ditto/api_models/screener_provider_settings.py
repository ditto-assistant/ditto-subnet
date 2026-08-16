"""Audited provider routing for independent screening lanes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ScreenerCapacityProvider = Literal["targon", "gcp"]


class ScreenerProviderSettings(BaseModel):
    """Ordered provider lists for build, runtime, and source-review lanes."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    runtime_provider_priority: tuple[ScreenerCapacityProvider, ...] = (
        "targon",
        "gcp",
    )
    source_review_provider_priority: tuple[ScreenerCapacityProvider, ...] = (
        "targon",
        "gcp",
    )
    build_provider_priority: tuple[ScreenerCapacityProvider, ...] = (
        "targon",
        "gcp",
    )

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
        return self

    def targon_runtime_enabled(self) -> bool:
        return self.runtime_provider_priority[0] == "targon"

    def targon_source_review_enabled(self) -> bool:
        return self.source_review_provider_priority[0] == "targon"

    def targon_builders_enabled(self) -> bool:
        return self.build_provider_priority[0] == "targon"

    def all_lanes_targon_first(self) -> bool:
        return (
            self.targon_runtime_enabled()
            and self.targon_source_review_enabled()
            and self.targon_builders_enabled()
        )

    def all_lanes_gcp_only(self) -> bool:
        return (
            not self.targon_runtime_enabled()
            and not self.targon_source_review_enabled()
            and not self.targon_builders_enabled()
        )


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
    return (
        f"APPLY SCREENER PROVIDERS BUILDS={builds} RUNTIME={runtime} "
        f"SOURCE_REVIEW={source_review}"
    )
