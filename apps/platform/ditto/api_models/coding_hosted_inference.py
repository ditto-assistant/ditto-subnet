"""Native hosted v2 policy and private provider accounting, never v1 tickets."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_inference import (
    CanonicalUUID,
    CodingInferenceLockedRequest,
    CodingInferenceSystemMessage,
    CodingInferenceSystemPrompt,
    CodingInferenceToolSchema,
    coding_inference_digest,
    parse_coding_inference_json,
    system_prompt_digest,
    tool_schema_digest,
)

Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
TokenLimit = Annotated[int, Field(strict=True, ge=1, le=2_250_000)]


class HostedInferencePolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    schema_name: Literal["dittobench-coding-hosted-inference-policy-v2"] = Field(
        alias="schema"
    )
    model: Literal["openai/gpt-5.6-luna"]
    provider_api: Literal["openrouter"]
    provider_route: Literal["azure/eu"]
    receipt_provider: Literal["Azure"]
    provider_route_profile: Literal["luna-azure-eu-zdr-v1"]
    reasoning_effort: Literal["medium"]
    prompt_sha256: Digest
    tool_schema_sha256: Digest
    allow_fallbacks: Literal[False]
    stream: Literal[False]
    store: Literal[False]
    zdr: Literal[True]
    data_collection: Literal["deny"]
    require_parameters: Literal[True]
    provider_account_guardrail: Literal["openrouter_private_account_v1"]
    provider_pipeline_policy: Literal["no_plugins_no_transforms_v1"]
    provider_cache_policy: Literal["disabled_v1"]
    router_metadata_required: Literal[True]
    retry_policy: Literal["no_retries_v2"]
    max_requests: Annotated[int, Field(strict=True, ge=1, le=256)]
    max_prompt_tokens: TokenLimit
    max_completion_tokens: Annotated[int, Field(strict=True, ge=1, le=250_000)]
    max_completion_tokens_per_request: Annotated[
        int, Field(strict=True, ge=1, le=32768)
    ]
    max_cost_usd_micros: Annotated[int, Field(strict=True, ge=1, le=100_000_000)]
    request_timeout_milliseconds: Annotated[int, Field(strict=True, ge=1000, le=300000)]

    @field_validator(
        "allow_fallbacks",
        "stream",
        "store",
        "zdr",
        "require_parameters",
        "router_metadata_required",
        mode="before",
    )
    @classmethod
    def strict_flags(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("hosted inference flags must be Boolean")
        return value

    @model_validator(mode="after")
    def limits(self) -> HostedInferencePolicy:
        if (
            self.max_completion_tokens_per_request > self.max_completion_tokens
            or self.request_timeout_milliseconds % 1000
        ):
            raise ValueError("hosted inference policy limits disagree")
        return self

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=16384,
            label="hosted inference policy",
        )

    def locked_request(self, body: bytes) -> tuple[CodingInferenceLockedRequest, str]:
        # Chat payload/prompt/tool ABIs are version-neutral model inputs. No
        # legacy grant, ticket, settlement or retry state is reused here.
        request = parse_coding_inference_json(
            CodingInferenceLockedRequest, body, maximum_bytes=4 << 20
        )
        if (
            request.model != self.model
            or request.reasoning.effort != self.reasoning_effort
            or not request.reasoning.exclude
            or request.provider.only != [self.provider_route]
            or request.provider.order != [self.provider_route]
            or request.max_completion_tokens > self.max_completion_tokens_per_request
            or not request.messages
            or not isinstance(request.messages[0], CodingInferenceSystemMessage)
        ):
            raise ValueError("hosted request disagrees with model policy")
        if (
            system_prompt_digest(
                CodingInferenceSystemPrompt(
                    schema="dittobench-coding-system-prompt-v1",
                    content=request.messages[0].content,
                )
            )
            != self.prompt_sha256
            or tool_schema_digest(
                CodingInferenceToolSchema(
                    schema="dittobench-coding-model-tools-v1", tools=request.tools
                )
            )
            != self.tool_schema_sha256
        ):
            raise ValueError("hosted prompt or tool policy does not match")
        return request, coding_inference_digest(request)


class HostedInferenceSettlement(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    schema_name: Literal["dittobench-coding-hosted-inference-settlement-v2"] = Field(
        alias="schema"
    )
    evaluation_id: CanonicalUUID
    attempt_id: CanonicalUUID
    grant_id: CanonicalUUID
    request_id: CanonicalUUID
    sequence: Annotated[int, Field(strict=True, ge=1, le=256)]
    policy_sha256: Digest
    locked_request_sha256: Digest
    response_sha256: Digest
    provider_receipt_sha256: Digest
    model: Literal["openai/gpt-5.6-luna"]
    provider: Literal["Azure"]
    provider_route: Literal["azure/eu"]
    provider_route_profile: Literal["luna-azure-eu-zdr-v1"]
    fallback_used: Literal[False]
    prompt_tokens: Annotated[int, Field(strict=True, ge=0, le=2_250_000)]
    completion_tokens: Annotated[int, Field(strict=True, ge=0, le=250_000)]
    cost_usd_micros: Annotated[int, Field(strict=True, ge=0, le=100_000_000)]

    @field_validator("fallback_used", mode="before")
    @classmethod
    def fallback_is_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("hosted fallback flag must be Boolean")
        return value

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=4096,
            label="hosted inference settlement",
        )
