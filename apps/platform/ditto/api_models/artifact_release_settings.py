"""Audited operator settings for public source release."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.source_disclosure import SourceDisclosure


class ArtifactReleaseSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    disclosure: SourceDisclosure = SourceDisclosure.PUBLIC
    embargo_hours: int
    """The window, in hours. Retained and inert while ``disclosure`` is
    ``never`` -- so returning to ``public`` restores the window the subnet last
    agreed on instead of forcing one to be re-chosen under time pressure."""

    reason: str
    actor: str
    created_at: datetime | None


class AdminArtifactReleaseSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: ArtifactReleaseSettingsRevision
    history: list[ArtifactReleaseSettingsRevision]


class AdminArtifactReleaseSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    # `strict=False` on this one field: the model is strict so an operator
    # cannot smuggle "1" past an int bound, but a JSON string is the only way
    # to send an enum over the wire, and strict mode rejects it as not being an
    # instance of the class. Unknown strings are still 422 -- the enum is the
    # validator, strictness was never what rejected them.
    disclosure: Annotated[SourceDisclosure, Field(strict=False)] = (
        SourceDisclosure.PUBLIC
    )
    # A range bound, not a recommendation. The ceiling is one year because a
    # year is the longest *finite* window anyone has proposed; past it the
    # honest value is `never`, which is now expressible, so there is no gap
    # between the top of the range and the terminal option.
    embargo_hours: Annotated[int, Field(ge=6, le=8760)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str
