"""Historical aggregate receipt models; no case execution or provisioning."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol.v13_private_package import V13PrivateRunSummary

_SHA_PATTERN = r"^[0-9a-f]{64}$"


class PrivatePairCounts(BaseModel):
    """Sanitized aggregate; no prompts, answers, case IDs, or hidden seeds."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    transformation_class: str
    seed_commitment: str = Field(pattern=_SHA_PATTERN)
    pairs: int = Field(ge=0)
    control_correct: int = Field(ge=0)
    variant_correct: int = Field(ge=0)
    control_only_correct: int = Field(ge=0)
    variant_only_correct: int = Field(ge=0)


class PrivateExecutionResult(BaseModel):
    """Report-only paired outcomes; no materiality decision or policy verdict."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: V13PrivateRunSummary
    aggregates: tuple[PrivatePairCounts, ...]
