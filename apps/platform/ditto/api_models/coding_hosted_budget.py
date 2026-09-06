"""Reviewed runtime assumptions, not provider discovery or activation authority."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.coding_canonical import (
    coding_canonical_json_bytes,
    coding_canonical_sha256,
)
from ditto.api_models.coding_hosted_inference import Digest


class HostedBudgetProfile(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, populate_by_name=True
    )

    schema_name: Literal["dittobench-coding-hosted-budget-profile-v2"] = Field(
        alias="schema"
    )
    algorithm: Literal["provider-billed-input-cap-v1"]
    model: Literal["openai/gpt-5.6-luna"]
    provider_api: Literal["openrouter"]
    provider_route: Literal["azure/eu"]
    provider_route_profile: Literal["luna-azure-eu-zdr-v1"]
    # These reference reviewed deployment/account and billing evidence, including
    # rejected/error requests. A context-window listing alone is insufficient.
    provider_review_sha256: Digest
    token_accounting_review_sha256: Digest
    pricing_review_sha256: Digest
    valid_from_unix: Annotated[int, Field(gt=0)]
    valid_until_unix: Annotated[int, Field(gt=0)]
    max_billed_prompt_tokens_per_request: Annotated[int, Field(ge=1, le=2_250_000)]
    max_completion_tokens_per_request: Annotated[int, Field(ge=1, le=32768)]
    prompt_price_usd_nanos_per_token: Annotated[int, Field(ge=1, le=1_000_000_000)]
    completion_price_usd_nanos_per_token: Annotated[int, Field(ge=1, le=1_000_000_000)]
    fixed_charge_usd_micros_per_request: Annotated[int, Field(ge=0, le=100_000_000)]

    @model_validator(mode="after")
    def validity(self) -> "HostedBudgetProfile":
        if not 0 < self.valid_until_unix - self.valid_from_unix <= 86400:
            raise ValueError("budget profile validity must be at most one day")
        if self.cost_ceiling(self.max_completion_tokens_per_request) > 100_000_000:
            raise ValueError("budget profile exceeds supported reservation ceiling")
        return self

    def cost_ceiling(self, completion_tokens: int) -> int:
        if (
            type(completion_tokens) is not int
            or not 1 <= completion_tokens <= self.max_completion_tokens_per_request
        ):
            raise ValueError("completion bound is outside the reviewed profile")
        return self.cost_for_usage(
            self.max_billed_prompt_tokens_per_request, completion_tokens
        )

    def cost_for_usage(self, prompt_tokens: int, completion_tokens: int) -> int:
        if (
            type(prompt_tokens) is not int
            or type(completion_tokens) is not int
            or not 0 <= prompt_tokens <= self.max_billed_prompt_tokens_per_request
            or not 0 <= completion_tokens <= self.max_completion_tokens_per_request
        ):
            raise ValueError("usage exceeds reviewed token bounds")
        # Round each component up separately, never assume a cache discount,
        # and include the reviewed upper bound for all other request charges.
        return (
            (prompt_tokens * self.prompt_price_usd_nanos_per_token + 999) // 1000
            + (completion_tokens * self.completion_price_usd_nanos_per_token + 999)
            // 1000
            + self.fixed_charge_usd_micros_per_request
        )

    def canonical_bytes(self) -> bytes:
        return coding_canonical_json_bytes(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=16384,
            label="budget profile",
        )

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=16384,
            label="budget profile",
        )
