"""Canonical shadow-only DittoBench Coding inference contracts.

The models in this module contain policy and synthetic evidence only. Runtime
bearers, provider credentials, usable capability URLs, and private prompts do
not belong in these projections.
"""

# ruff: noqa: UP047 -- Platform mirrors this module and still supports Python 3.11.

from __future__ import annotations

import hashlib
import json
import unicodedata
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_CANONICAL_INFERENCE_BYTES = 8 << 20
MAX_INFERENCE_POLICY_BYTES = 64 << 10
MAX_INFERENCE_REQUEST_BYTES = 4 << 20
MAX_INFERENCE_RECEIPT_SET_BYTES = 4 << 20
MAX_INFERENCE_JSON_DEPTH = 32
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_UINT64_MAX = (1 << 64) - 1


class CodingInferenceModel(BaseModel):
    """Immutable forward-compatible known-field projection."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        strict=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )


class CodingInferenceRequestModel(CodingInferenceModel):
    """Raw request parsing applies an exact-key allowlist before projection."""


Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
BoundedName = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(lambda value: _bounded_identifier(value, 128, "short name")),
]
BoundedIdentity = Annotated[
    str,
    Field(min_length=1, max_length=256),
    AfterValidator(lambda value: _bounded_identifier(value, 256, "identity")),
]
ModelVisibleText = Annotated[
    str,
    Field(max_length=MAX_INFERENCE_REQUEST_BYTES),
    AfterValidator(
        lambda value: _bounded_utf8(
            value, MAX_INFERENCE_REQUEST_BYTES, "model-visible message"
        )
    ),
]
ResponseText = Annotated[
    str,
    Field(max_length=MAX_CANONICAL_INFERENCE_BYTES),
    AfterValidator(
        lambda value: _bounded_utf8(
            value, MAX_CANONICAL_INFERENCE_BYTES, "model response content"
        )
    ),
]


def _canonical_uuid(value: Any) -> Any:
    if isinstance(value, UUID):
        if value.int == 0:
            raise ValueError("coding inference UUID is nil")
        return value
    if not isinstance(value, str):
        raise ValueError("coding inference UUID must be a canonical string")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError("coding inference UUID is invalid") from error
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError("coding inference UUID is not canonical")
    return value


CanonicalUUID = Annotated[UUID, BeforeValidator(_canonical_uuid)]


def _bounded_utf8(value: str, maximum: int, label: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} is not valid UTF-8") from error
    if len(encoded) > maximum:
        raise ValueError(f"{label} is outside its UTF-8 bound")
    return value


def _bounded_identifier(value: str, maximum: int, label: str) -> str:
    value = _bounded_utf8(value, maximum, label)
    if any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError(f"{label} contains whitespace or control characters")
    return value


def _json_depth_and_unicode(value: Any, depth: int = 0) -> None:
    if depth > MAX_INFERENCE_JSON_DEPTH:
        raise ValueError("coding inference JSON exceeds the depth limit")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(
                "coding inference JSON contains invalid Unicode"
            ) from error
        return
    if value is None or type(value) in {bool, int, float}:
        return
    if isinstance(value, list):
        for item in value:
            _json_depth_and_unicode(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("coding inference JSON object key is not a string")
            _json_depth_and_unicode(key, depth)
            _json_depth_and_unicode(item, depth + 1)
        return
    raise ValueError("coding inference value is not JSON-compatible")


def _json_uses_canonical_integers(value: Any) -> bool:
    if value is None or type(value) in {bool, str}:
        return True
    if type(value) is int:
        return -(1 << 63) <= value <= (1 << 63) - 1
    if isinstance(value, list):
        return all(_json_uses_canonical_integers(item) for item in value)
    if isinstance(value, dict):
        return all(_json_uses_canonical_integers(item) for item in value.values())
    return False


def _reject_constant(value: str) -> None:
    raise ValueError(f"coding inference JSON contains non-finite number {value}")


def _parse_json_int(value: str) -> int:
    digits = value.removeprefix("-")
    if value == "-0" or len(digits) > 100:
        raise ValueError("coding inference JSON integer spelling is outside bounds")
    return int(value)


def _parse_json_float(value: str) -> float:
    if len(value) > 64:
        raise ValueError("coding inference JSON decimal spelling is outside bounds")
    if "e" in value.lower():
        exponent = int(value.lower().split("e", 1)[1])
        if exponent < -100 or exponent > 100:
            raise ValueError("coding inference JSON decimal exponent is outside bounds")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed.adjusted() < -100 or parsed.adjusted() > 100:
        raise ValueError("coding inference JSON decimal exponent is outside bounds")
    return float(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} contains missing or unsupported fields")
    return value


def _validate_tool_call_shape(value: Any, label: str) -> None:
    call = _exact_keys(value, {"id", "type", "function"}, label)
    _exact_keys(call["function"], {"name", "arguments"}, f"{label}.function")


def _validate_tool_shape(value: Any, label: str) -> None:
    tool = _exact_keys(value, {"type", "function"}, label)
    _exact_keys(
        tool["function"],
        {"name", "description", "parameters"},
        f"{label}.function",
    )


def _validate_message_shape(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    role = value.get("role")
    if role in {"system", "user"}:
        _exact_keys(value, {"role", "content"}, label)
    elif role == "assistant":
        message = _exact_keys(value, {"role", "content", "tool_calls"}, label)
        if not isinstance(message["tool_calls"], list):
            raise ValueError(f"{label}.tool_calls is not an array")
        for index, call in enumerate(message["tool_calls"]):
            _validate_tool_call_shape(call, f"{label}.tool_calls[{index}]")
    elif role == "tool":
        _exact_keys(value, {"role", "tool_call_id", "content"}, label)
    else:
        raise ValueError(f"{label}.role is invalid")


def _validate_model_visible_shape(model: type[BaseModel], value: Any) -> None:
    if model is CodingInferenceToolSchema:
        root = value if isinstance(value, dict) else {}
        for index, tool in enumerate(root.get("tools", [])):
            _validate_tool_shape(tool, f"tools[{index}]")
        return
    if model is not CodingInferenceLockedRequest:
        return
    request = _exact_keys(
        value,
        {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "reasoning",
            "max_completion_tokens",
            "parallel_tool_calls",
            "n",
            "stream",
            "store",
            "usage",
            "provider",
        },
        "locked request",
    )
    _exact_keys(request["reasoning"], {"effort", "exclude"}, "reasoning")
    _exact_keys(request["usage"], {"include"}, "usage")
    _exact_keys(
        request["provider"],
        {
            "only",
            "order",
            "allow_fallbacks",
            "require_parameters",
            "data_collection",
            "zdr",
        },
        "provider",
    )
    if not isinstance(request["messages"], list) or not isinstance(
        request["tools"], list
    ):
        raise ValueError("locked request messages/tools are not arrays")
    for index, message in enumerate(request["messages"]):
        _validate_message_shape(message, f"messages[{index}]")
    for index, tool in enumerate(request["tools"]):
        _validate_tool_shape(tool, f"tools[{index}]")


ModelT = TypeVar("ModelT", bound=BaseModel)


def _decode_json_document(
    body: bytes,
    *,
    maximum_bytes: int = MAX_CANONICAL_INFERENCE_BYTES,
) -> Any:
    if maximum_bytes <= 0 or not body or len(body) > maximum_bytes:
        raise ValueError("coding inference JSON size is outside its bound")
    try:
        text = body.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_int=_parse_json_int,
            parse_float=_parse_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("coding inference JSON is invalid") from error
    _json_depth_and_unicode(value)
    return value


def parse_coding_inference_json(
    model: type[ModelT],
    body: bytes,
    *,
    maximum_bytes: int | None = None,
) -> ModelT:
    """Parse one bounded document with strict raw-JSON safety checks."""

    maximum = (
        _inference_model_maximum(model) if maximum_bytes is None else maximum_bytes
    )
    decoded = _decode_json_document(body, maximum_bytes=maximum)
    _validate_model_visible_shape(model, decoded)
    try:
        return model.model_validate_json(body)
    except ValidationError as error:
        raise ValueError(
            "coding inference JSON violates its known-field schema"
        ) from error


def coding_inference_canonical_json_bytes(
    value: BaseModel | dict[str, Any] | list[Any],
    *,
    maximum_bytes: int = MAX_CANONICAL_INFERENCE_BYTES,
) -> bytes:
    """Encode one validated known-field projection using shared canonical JSON."""

    projected: dict[str, Any] | list[Any]
    if isinstance(value, BaseModel):
        normalized = type(value).model_validate_json(
            value.model_dump_json(by_alias=True)
        )
        projected = normalized.model_dump(mode="json", by_alias=True)
    else:
        projected = value
    _json_depth_and_unicode(projected)
    try:
        body = (
            (
                json.dumps(
                    projected,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
            .encode()
        )
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ValueError("coding inference projection is not canonical JSON") from error
    if len(body) > maximum_bytes:
        raise ValueError("canonical coding inference JSON exceeds its bound")
    return body


def coding_inference_digest(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    maximum = (
        _inference_model_maximum(type(value))
        if isinstance(value, BaseModel)
        else MAX_CANONICAL_INFERENCE_BYTES
    )
    return hashlib.sha256(
        coding_inference_canonical_json_bytes(value, maximum_bytes=maximum)
    ).hexdigest()


class CodingInferenceSystemPrompt(CodingInferenceModel):
    schema_name: str = Field(
        alias="schema", pattern=r"^dittobench-coding-system-prompt-v1$"
    )
    content: Annotated[str, Field(min_length=1, max_length=64 * 1024)]

    @field_validator("content")
    @classmethod
    def content_is_bounded_utf8(cls, value: str) -> str:
        return _bounded_utf8(value, 64 * 1024, "coding system prompt")


class CodingInferenceFunction(CodingInferenceRequestModel):
    name: BoundedName
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    parameters: dict[str, Any]

    @field_validator("name")
    @classmethod
    def name_is_bounded(cls, value: str) -> str:
        return _bounded_identifier(value, 128, "coding tool name")

    @field_validator("description")
    @classmethod
    def description_is_bounded(cls, value: str) -> str:
        return _bounded_utf8(value, 2000, "coding tool description")

    @field_validator("parameters")
    @classmethod
    def parameters_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _json_depth_and_unicode(value)
        if not _json_uses_canonical_integers(value):
            raise ValueError("coding tool parameters require canonical integers")
        if len(coding_inference_canonical_json_bytes(value)) > 64 << 10:
            raise ValueError("coding tool parameters exceed 64 KiB")
        return value


class CodingInferenceTool(CodingInferenceRequestModel):
    type: str = Field(pattern=r"^function$")
    function: CodingInferenceFunction


class CodingInferenceToolSchema(CodingInferenceModel):
    schema_name: str = Field(
        alias="schema", pattern=r"^dittobench-coding-model-tools-v1$"
    )
    tools: Annotated[list[CodingInferenceTool], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def tool_names_are_unique(self) -> CodingInferenceToolSchema:
        names = [tool.function.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("coding tool names must be unique")
        return self


class CodingInferenceToolCallFunction(CodingInferenceRequestModel):
    name: BoundedName
    arguments: Annotated[str, Field(min_length=1, max_length=64 << 10)]

    @field_validator("arguments")
    @classmethod
    def arguments_are_json(cls, value: str) -> str:
        decoded = _decode_json_document(
            value.encode("utf-8"),
            maximum_bytes=64 << 10,
        )
        if not isinstance(decoded, dict):
            raise ValueError("coding tool arguments must be a JSON object")
        return value


class CodingInferenceToolCall(CodingInferenceRequestModel):
    id: BoundedIdentity
    type: str = Field(pattern=r"^function$")
    function: CodingInferenceToolCallFunction


class CodingInferenceSystemMessage(CodingInferenceRequestModel):
    role: Literal["system"]
    content: ModelVisibleText


class CodingInferenceUserMessage(CodingInferenceRequestModel):
    role: Literal["user"]
    content: ModelVisibleText


class CodingInferenceAssistantMessage(CodingInferenceRequestModel):
    role: Literal["assistant"]
    content: ModelVisibleText | None
    tool_calls: Annotated[list[CodingInferenceToolCall], Field(max_length=1)]


class CodingInferenceToolMessage(CodingInferenceRequestModel):
    role: Literal["tool"]
    tool_call_id: BoundedIdentity
    content: ModelVisibleText


CodingInferenceMessage = Annotated[
    CodingInferenceSystemMessage
    | CodingInferenceUserMessage
    | CodingInferenceAssistantMessage
    | CodingInferenceToolMessage,
    Field(discriminator="role"),
]


class CodingInferenceReasoning(CodingInferenceRequestModel):
    effort: str = Field(pattern=r"^medium$")
    exclude: bool

    @field_validator("exclude")
    @classmethod
    def exclude_is_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("coding inference reasoning must be excluded")
        return value


class CodingInferenceUsageRequest(CodingInferenceRequestModel):
    include: bool

    @field_validator("include")
    @classmethod
    def include_is_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("coding inference usage must be included")
        return value


class CodingInferenceProviderRequest(CodingInferenceRequestModel):
    only: Annotated[list[str], Field(min_length=1, max_length=1)]
    order: Annotated[list[str], Field(min_length=1, max_length=1)]
    allow_fallbacks: bool
    require_parameters: bool
    data_collection: str = Field(pattern=r"^deny$")
    zdr: bool

    @model_validator(mode="after")
    def routing_is_locked(self) -> CodingInferenceProviderRequest:
        if (
            self.only != self.order
            or self.allow_fallbacks
            or not self.require_parameters
            or not self.zdr
        ):
            raise ValueError("coding inference provider routing is not locked")
        return self


class CodingInferenceLockedRequest(CodingInferenceRequestModel):
    model: str = Field(pattern=r"^openai/gpt-5\.6-luna$")
    messages: Annotated[
        list[CodingInferenceMessage], Field(min_length=1, max_length=512)
    ]
    tools: Annotated[list[CodingInferenceTool], Field(min_length=1, max_length=64)]
    tool_choice: str = Field(pattern=r"^auto$")
    reasoning: CodingInferenceReasoning
    max_completion_tokens: Annotated[int, Field(ge=1, le=32_768)]
    parallel_tool_calls: bool
    n: Annotated[int, Field(ge=1, le=1)]
    stream: bool
    store: bool
    usage: CodingInferenceUsageRequest
    provider: CodingInferenceProviderRequest

    @model_validator(mode="after")
    def scalar_policy_is_locked(self) -> CodingInferenceLockedRequest:
        if self.parallel_tool_calls or self.stream or self.store:
            raise ValueError("coding inference request enables forbidden behavior")
        names = [tool.function.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("coding request tool names must be unique")
        return self


class CodingInferencePolicy(CodingInferenceModel):
    schema_name: str = Field(
        alias="schema", pattern=r"^dittobench-coding-inference-policy-v1$"
    )
    coding_contract_version: Annotated[int, Field(ge=1, le=1)]
    bench_family: str = Field(pattern=r"^coding$")
    weight_eligible: bool
    api: str = Field(pattern=r"^openai-compatible-chat-completions$")
    model: str = Field(pattern=r"^openai/gpt-5\.6-luna$")
    provider_api: str = Field(pattern=r"^openrouter$")
    provider_route: BoundedName
    receipt_provider: BoundedName
    provider_receipt_source: Literal["platform_settlement_v1"]
    provider_account_guardrail: Literal["openrouter_private_account_v1"]
    provider_pipeline_policy: Literal["no_plugins_no_transforms_v1"]
    provider_cache_policy: Literal["disabled_v1"]
    router_metadata_required: Literal[True]
    provider_route_profile: BoundedName
    prompt_sha256: Sha256
    tool_schema_sha256: Sha256
    reasoning_effort: str = Field(pattern=r"^medium$")
    reasoning_excluded: bool
    stream: bool
    store: bool
    n: Annotated[int, Field(ge=1, le=1)]
    parallel_tool_calls: bool
    max_tool_calls_per_response: Annotated[int, Field(ge=1, le=1)]
    usage_included: bool
    allow_fallbacks: bool
    require_parameters: bool
    data_collection: str = Field(pattern=r"^deny$")
    zdr: bool
    max_requests: Annotated[int, Field(ge=256, le=256)]
    max_prompt_tokens: Annotated[int, Field(ge=1, le=2_000_000)]
    max_completion_tokens: Annotated[int, Field(ge=1, le=250_000)]
    max_total_tokens: Annotated[int, Field(ge=1, le=2_250_000)]
    max_completion_tokens_per_request: Annotated[int, Field(ge=1, le=32_768)]
    max_cost_usd_micros: Annotated[int, Field(ge=1, le=100_000_000)]
    max_request_bytes: Annotated[int, Field(ge=1, le=4 << 20)]
    max_response_bytes: Annotated[int, Field(ge=1, le=8 << 20)]
    request_timeout_milliseconds: Annotated[int, Field(ge=1000, le=300_000)]
    retry_policy: str = Field(pattern=r"^receipt_free_pre_provider_v1$")
    max_attempts_per_request: Annotated[int, Field(ge=1, le=3)]
    max_retries: Annotated[int, Field(ge=0, le=100)]
    cost_source: str = Field(pattern=r"^provider_receipt_v1$")
    currency: str = Field(pattern=r"^USD$")

    @model_validator(mode="after")
    def policy_is_fail_closed(self) -> CodingInferencePolicy:
        flags = (
            self.reasoning_excluded,
            not self.stream,
            not self.store,
            not self.parallel_tool_calls,
            self.usage_included,
            not self.allow_fallbacks,
            self.require_parameters,
            self.zdr,
            not self.weight_eligible,
        )
        if not all(flags):
            raise ValueError("coding inference policy enables forbidden behavior")
        if self.max_completion_tokens_per_request > self.max_completion_tokens:
            raise ValueError("per-request output exceeds the aggregate output budget")
        if self.max_total_tokens != (
            self.max_prompt_tokens + self.max_completion_tokens
        ):
            raise ValueError("coding inference total-token budget is incoherent")
        if self.max_request_bytes != 4 << 20 or self.max_response_bytes != 8 << 20:
            raise ValueError("coding inference transport bounds are not canonical")
        if self.request_timeout_milliseconds % 1000:
            raise ValueError("coding inference timeout must use whole seconds")
        if self.max_requests + self.max_retries > 1100:
            raise ValueError("coding inference retry budget is incoherent")
        return self


class CodingInferenceResponseUsage(CodingInferenceModel):
    prompt_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    completion_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    total_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    cost_usd_micros: Annotated[int, Field(ge=0, le=_UINT64_MAX)]

    @model_validator(mode="after")
    def totals_are_coherent(self) -> CodingInferenceResponseUsage:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("coding inference response token totals disagree")
        return self


class CodingInferenceResponseMessage(CodingInferenceModel):
    content: ResponseText | None
    tool_calls: Annotated[list[CodingInferenceToolCall], Field(max_length=1)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def response_has_content_or_tools(self) -> CodingInferenceResponseMessage:
        if self.content is None and not self.tool_calls:
            raise ValueError("coding inference response has no content or tool call")
        call_ids = [call.id for call in self.tool_calls]
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("coding inference response tool-call IDs are not unique")
        return self


class CodingInferenceResponseChoice(CodingInferenceModel):
    message: CodingInferenceResponseMessage


class CodingInferenceNormalizedResponse(CodingInferenceModel):
    schema_name: str = Field(
        alias="schema", pattern=r"^dittobench-coding-inference-response-v1$"
    )
    id: BoundedIdentity
    model: str = Field(pattern=r"^openai/gpt-5\.6-luna$")
    provider: BoundedName
    choices: Annotated[
        list[CodingInferenceResponseChoice], Field(min_length=1, max_length=1)
    ]
    usage: CodingInferenceResponseUsage


class CodingInferenceProviderUsage(CodingInferenceModel):
    prompt_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    completion_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    total_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    cost: Annotated[Decimal, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def totals_are_coherent(self) -> CodingInferenceProviderUsage:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("coding provider response token totals disagree")
        return self


class CodingInferenceProviderResponse(CodingInferenceModel):
    id: BoundedIdentity
    model: str = Field(pattern=r"^openai/gpt-5\.6-luna$")
    provider: BoundedName
    choices: Annotated[
        list[CodingInferenceResponseChoice], Field(min_length=1, max_length=1)
    ]
    usage: CodingInferenceProviderUsage


class CodingInferenceReceiptOutcome(StrEnum):
    COMPLETE = "complete"
    RECEIPT_FREE_RETRY = "receipt_free_retry"
    PROVIDER_FAILURE = "provider_failure"


class CodingInferenceReceipt(CodingInferenceModel):
    schema_name: str = Field(
        alias="schema", pattern=r"^dittobench-coding-inference-receipt-v1$"
    )
    sequence: Annotated[int, Field(ge=1, le=1100)]
    request_sequence: Annotated[int, Field(ge=1, le=1000)]
    attempt: Annotated[int, Field(ge=1, le=3)]
    request_id: CanonicalUUID
    locked_request_sha256: Sha256
    prompt_sha256: Sha256
    tool_schema_sha256: Sha256
    outcome: CodingInferenceReceiptOutcome
    failure_code: BoundedName | None
    http_status: Annotated[int, Field(ge=0, le=599)]
    response_sha256: Sha256 | None
    response_digest_kind: Literal["none", "normalized_v1", "canonical_json_v1"]
    provider_generation_id: BoundedIdentity | None
    provider_settlement_sha256: Sha256
    model: str = Field(pattern=r"^openai/gpt-5\.6-luna$")
    provider_route: BoundedName
    provider_route_profile: BoundedName
    provider_selected: bool
    receipt_provider: BoundedName | None
    fallback_used: bool
    prompt_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    completion_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    total_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    cost_usd_micros: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    timed_out: bool

    @model_validator(mode="after")
    def outcome_is_coherent(self) -> CodingInferenceReceipt:
        if self.request_id.int == 0 or self.fallback_used:
            raise ValueError("coding inference receipt identity is invalid")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("coding inference receipt token totals disagree")
        zero_accounting = (
            self.prompt_tokens == 0
            and self.completion_tokens == 0
            and self.total_tokens == 0
            and self.cost_usd_micros == 0
        )
        digest_shape = (
            self.response_digest_kind == "none"
            if self.response_sha256 is None
            else (
                self.response_digest_kind == "normalized_v1"
                if self.outcome is CodingInferenceReceiptOutcome.COMPLETE
                else self.response_digest_kind == "canonical_json_v1"
            )
        )
        if self.outcome is CodingInferenceReceiptOutcome.COMPLETE:
            valid = (
                self.failure_code is None
                and 200 <= self.http_status < 300
                and self.response_sha256 is not None
                and self.provider_selected
                and self.receipt_provider is not None
                and self.provider_generation_id is not None
                and not self.timed_out
            )
        elif self.outcome is CodingInferenceReceiptOutcome.RECEIPT_FREE_RETRY:
            valid = (
                self.failure_code == "pre_provider_unavailable"
                and self.http_status in {404, 408, 429, 500, 502, 503, 504}
                and self.response_sha256 is None
                and not self.provider_selected
                and self.receipt_provider is None
                and self.provider_generation_id is None
                and zero_accounting
                and not self.timed_out
            )
        else:
            failure_shape = {
                "pre_provider_unavailable": (
                    not self.provider_selected
                    and self.http_status in {404, 408, 429, 500, 502, 503, 504}
                    and self.response_sha256 is None
                    and not self.timed_out
                ),
                "provider_timeout": (
                    self.timed_out and self.http_status in {0, 408, 504}
                ),
                "provider_transport": (
                    not self.timed_out
                    and self.http_status == 0
                    and self.response_sha256 is None
                ),
                "provider_http": (not self.timed_out and self.http_status >= 400),
                "provider_response_invalid": (
                    not self.timed_out
                    and 200 <= self.http_status < 300
                    and self.response_sha256 is not None
                ),
            }.get(self.failure_code or "", False)
            provider_shape = (
                self.provider_selected and self.receipt_provider is not None
            ) or (
                not self.provider_selected
                and self.receipt_provider is None
                and self.provider_generation_id is None
                and zero_accounting
            )
            valid = (
                self.failure_code
                in {
                    "pre_provider_unavailable",
                    "provider_http",
                    "provider_response_invalid",
                    "provider_timeout",
                    "provider_transport",
                }
                and failure_shape
                and provider_shape
            )
        if not valid or not digest_shape:
            raise ValueError("coding inference receipt outcome is incoherent")
        return self


class CodingInferenceRouterAttempt(CodingInferenceModel):
    provider: BoundedName
    selected: bool


class CodingInferenceProviderSettlement(CodingInferenceModel):
    schema_name: str = Field(
        alias="schema", pattern=r"^dittobench-coding-provider-settlement-v1$"
    )
    coding_contract_version: Annotated[int, Field(ge=1, le=1)]
    ticket_id: CanonicalUUID
    case_id: BoundedIdentity
    profile_capability_id: BoundedIdentity
    inference_grant_sha256: Sha256
    grant_id: CanonicalUUID
    generation: Annotated[int, Field(ge=1, le=(1 << 31) - 1)]
    request_id: CanonicalUUID
    request_sequence: Annotated[int, Field(ge=1, le=256)]
    attempt: Annotated[int, Field(ge=1, le=3)]
    locked_request_sha256: Sha256
    outcome: CodingInferenceReceiptOutcome
    terminal_error_code: BoundedName | None
    http_status: Annotated[int, Field(ge=0, le=599)]
    response_sha256: Sha256 | None
    response_digest_kind: Literal["none", "normalized_v1", "canonical_json_v1"]
    provider_generation_id: BoundedIdentity | None
    model: str = Field(pattern=r"^openai/gpt-5\.6-luna$")
    provider_api: Literal["openrouter"]
    provider_route: BoundedName
    receipt_provider: BoundedName | None
    provider_route_profile: BoundedName
    provider_account_guardrail: Literal["openrouter_private_account_v1"]
    provider_pipeline_policy: Literal["no_plugins_no_transforms_v1"]
    provider_cache_policy: Literal["disabled_v1"]
    router_metadata_verified: Literal[True]
    router_attempts: Annotated[
        list[CodingInferenceRouterAttempt], Field(min_length=1, max_length=1)
    ]
    pipeline_stages: Annotated[list[BoundedName], Field(max_length=0)]
    fallback_used: Literal[False]
    usage_available: bool
    prompt_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    completion_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    total_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    cost_available: bool
    cost_usd_micros: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    timed_out: bool

    @model_validator(mode="after")
    def accounting_is_coherent(self) -> CodingInferenceProviderSettlement:
        if (
            self.total_tokens != self.prompt_tokens + self.completion_tokens
            or (
                not self.usage_available
                and any((self.prompt_tokens, self.completion_tokens, self.total_tokens))
            )
            or (not self.cost_available and self.cost_usd_micros != 0)
        ):
            raise ValueError(
                "coding inference provider settlement accounting disagrees"
            )
        return self


class CodingInferenceReceiptSet(CodingInferenceModel):
    schema_name: str = Field(
        alias="schema", pattern=r"^dittobench-coding-inference-receipt-set-v1$"
    )
    coding_contract_version: Annotated[int, Field(ge=1, le=1)]
    ticket_id: CanonicalUUID
    case_id: BoundedIdentity
    profile_capability_id: BoundedIdentity
    grant_id: CanonicalUUID
    generation: Annotated[int, Field(ge=1, le=(1 << 31) - 1)]
    inference_grant_sha256: Sha256
    request_budget: Annotated[int, Field(ge=1, le=1000)]
    prompt_token_budget: Annotated[int, Field(ge=1, le=2_000_000)]
    completion_token_budget: Annotated[int, Field(ge=1, le=250_000)]
    receipts: Annotated[
        list[CodingInferenceReceipt], Field(min_length=1, max_length=1100)
    ]

    @model_validator(mode="after")
    def receipt_order_is_coherent(self) -> CodingInferenceReceiptSet:
        if self.ticket_id.int == 0 or self.grant_id.int == 0:
            raise ValueError("coding inference receipt-set UUID is nil")
        seen_request_ids: dict[UUID, int] = {}
        seen_settlements: set[str] = set()
        seen_generations: set[str] = set()
        for index, receipt in enumerate(self.receipts):
            if receipt.sequence != index + 1:
                raise ValueError("coding inference receipt sequence is not contiguous")
            if index == 0:
                if receipt.request_sequence != 1 or receipt.attempt != 1:
                    raise ValueError(
                        "coding inference receipt set has an invalid first event"
                    )
            else:
                previous = self.receipts[index - 1]
                if receipt.request_sequence == previous.request_sequence:
                    if (
                        previous.outcome
                        is not CodingInferenceReceiptOutcome.RECEIPT_FREE_RETRY
                        or receipt.attempt != previous.attempt + 1
                        or receipt.request_id != previous.request_id
                        or receipt.locked_request_sha256
                        != previous.locked_request_sha256
                        or receipt.prompt_sha256 != previous.prompt_sha256
                        or receipt.tool_schema_sha256 != previous.tool_schema_sha256
                    ):
                        raise ValueError("coding inference retry identity drifted")
                elif (
                    receipt.request_sequence != previous.request_sequence + 1
                    or receipt.attempt != 1
                    or previous.outcome is not CodingInferenceReceiptOutcome.COMPLETE
                ):
                    raise ValueError("coding inference request order is invalid")
            prior_sequence = seen_request_ids.setdefault(
                receipt.request_id, receipt.request_sequence
            )
            if prior_sequence != receipt.request_sequence:
                raise ValueError("coding inference request ID was reused")
            if receipt.provider_settlement_sha256 in seen_settlements:
                raise ValueError("coding inference provider settlement was reused")
            seen_settlements.add(receipt.provider_settlement_sha256)
            if receipt.provider_generation_id is not None:
                if receipt.provider_generation_id in seen_generations:
                    raise ValueError("coding inference provider generation was reused")
                seen_generations.add(receipt.provider_generation_id)
        if (
            self.receipts[-1].outcome
            is CodingInferenceReceiptOutcome.RECEIPT_FREE_RETRY
        ):
            raise ValueError("receipt-free retry cannot terminate a receipt set")
        return self


class CodingInferenceReceiptBinding(CodingInferenceModel):
    """Trusted lease/live-grant identity; receipt bytes cannot choose it."""

    ticket_id: CanonicalUUID
    case_id: BoundedIdentity
    profile_capability_id: BoundedIdentity
    grant_id: CanonicalUUID
    generation: Annotated[int, Field(ge=1, le=(1 << 31) - 1)]
    inference_grant_sha256: Sha256
    request_budget: Annotated[int, Field(ge=1, le=1000)]
    prompt_token_budget: Annotated[int, Field(ge=1, le=2_000_000)]
    completion_token_budget: Annotated[int, Field(ge=1, le=250_000)]


class CodingInferenceModelUsageStatus(StrEnum):
    COMPLETE = "complete"
    NOT_INVOKED = "not_invoked"
    PROVIDER_FAILURE = "provider_failure"


class CodingInferenceModelEvidence(CodingInferenceModel):
    model: str = Field(pattern=r"^openai/gpt-5\.6-luna$")
    provider: BoundedName
    provider_route_profile: BoundedName
    reasoning_effort: str = Field(pattern=r"^medium$")
    inference_grant_sha256: Sha256
    prompt_sha256: Sha256
    tool_schema_sha256: Sha256
    usage_status: CodingInferenceModelUsageStatus
    fallback_used: bool
    cost_source: str = Field(pattern=r"^provider_receipt_v1$")
    currency: str = Field(pattern=r"^USD$")
    provider_receipt_set_sha256: Sha256 | None
    requests: Annotated[int, Field(ge=0, le=1000)]
    prompt_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    completion_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    total_tokens: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    cost_usd_micros: Annotated[int, Field(ge=0, le=_UINT64_MAX)]
    retry_count: Annotated[int, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def accounting_is_coherent(self) -> CodingInferenceModelEvidence:
        if (
            self.fallback_used
            or self.total_tokens != self.prompt_tokens + self.completion_tokens
        ):
            raise ValueError("coding inference model evidence is incoherent")
        counters = (
            self.requests,
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
            self.cost_usd_micros,
            self.retry_count,
        )
        if self.usage_status is CodingInferenceModelUsageStatus.NOT_INVOKED:
            if any(counters) or self.provider_receipt_set_sha256 is not None:
                raise ValueError("not-invoked evidence has nonzero accounting")
        elif self.requests == 0 or self.provider_receipt_set_sha256 is None:
            raise ValueError("invoked evidence lacks a provider receipt set")
        return self


def _inference_model_maximum(model: type[BaseModel]) -> int:
    if issubclass(model, (CodingInferencePolicy, CodingInferenceSystemPrompt)):
        return MAX_INFERENCE_POLICY_BYTES
    if issubclass(model, (CodingInferenceLockedRequest, CodingInferenceToolSchema)):
        return MAX_INFERENCE_REQUEST_BYTES
    if issubclass(
        model, (CodingInferenceReceiptSet, CodingInferenceProviderSettlement)
    ):
        return MAX_INFERENCE_RECEIPT_SET_BYTES
    return MAX_CANONICAL_INFERENCE_BYTES


def system_prompt_digest(prompt: CodingInferenceSystemPrompt) -> str:
    return coding_inference_digest(prompt)


def tool_schema_digest(schema: CodingInferenceToolSchema) -> str:
    return coding_inference_digest(schema)


def policy_digest(policy: CodingInferencePolicy) -> str:
    return coding_inference_digest(policy)


def effective_inference_request_budget(workspace_tool_calls: int) -> int:
    if type(workspace_tool_calls) is not int or not 1 <= workspace_tool_calls <= 1000:
        raise ValueError("workspace tool-call budget is outside coding bounds")
    return min(workspace_tool_calls + 16, 256)


def locked_request_digest(
    request: CodingInferenceLockedRequest,
    policy: CodingInferencePolicy,
) -> str:
    if (
        request.model != policy.model
        or request.reasoning.effort != policy.reasoning_effort
        or request.reasoning.exclude != policy.reasoning_excluded
        or request.max_completion_tokens > policy.max_completion_tokens_per_request
        or request.parallel_tool_calls != policy.parallel_tool_calls
        or request.n != policy.n
        or request.stream != policy.stream
        or request.store != policy.store
        or request.usage.include != policy.usage_included
        or request.provider.only != [policy.provider_route]
        or request.provider.order != [policy.provider_route]
        or request.provider.allow_fallbacks != policy.allow_fallbacks
        or request.provider.require_parameters != policy.require_parameters
        or request.provider.data_collection != policy.data_collection
        or request.provider.zdr != policy.zdr
    ):
        raise ValueError("locked request disagrees with coding inference policy")
    schema = CodingInferenceToolSchema(
        schema="dittobench-coding-model-tools-v1", tools=request.tools
    )
    if tool_schema_digest(schema) != policy.tool_schema_sha256:
        raise ValueError("locked request tool schema disagrees with policy")
    if not request.messages or not isinstance(
        request.messages[0], CodingInferenceSystemMessage
    ):
        raise ValueError("locked request lacks its system prompt")
    prompt = CodingInferenceSystemPrompt(
        schema="dittobench-coding-system-prompt-v1",
        content=request.messages[0].content,
    )
    if system_prompt_digest(prompt) != policy.prompt_sha256:
        raise ValueError("locked request system prompt disagrees with policy")
    return coding_inference_digest(request)


def normalized_response_digest(
    response: CodingInferenceNormalizedResponse,
    policy: CodingInferencePolicy,
) -> str:
    if (
        response.model != policy.model
        or response.provider != policy.receipt_provider
        or response.usage.prompt_tokens > policy.max_prompt_tokens
        or response.usage.completion_tokens > policy.max_completion_tokens_per_request
        or response.usage.total_tokens > policy.max_total_tokens
        or response.usage.cost_usd_micros > policy.max_cost_usd_micros
    ):
        raise ValueError("normalized provider response disagrees with policy")
    return coding_inference_digest(response)


def normalize_provider_response(
    response: CodingInferenceProviderResponse,
    policy: CodingInferencePolicy,
) -> CodingInferenceNormalizedResponse:
    if response.model != policy.model or response.provider != policy.receipt_provider:
        raise ValueError("provider response identity disagrees with policy")
    micros = int(
        (response.usage.cost * Decimal(1_000_000)).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
    )
    if micros > policy.max_cost_usd_micros:
        raise ValueError("provider response cost exceeds policy")
    normalized = CodingInferenceNormalizedResponse(
        schema="dittobench-coding-inference-response-v1",
        id=response.id,
        model=response.model,
        provider=response.provider,
        choices=response.choices,
        usage=CodingInferenceResponseUsage(
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            cost_usd_micros=micros,
        ),
    )
    normalized_response_digest(normalized, policy)
    return normalized


def _receipt_policy_aggregate(
    policy: CodingInferencePolicy,
    receipts: CodingInferenceReceiptSet,
) -> tuple[list[CodingInferenceReceipt], int, int, int, int, int]:
    if receipts.inference_grant_sha256 != policy_digest(policy):
        raise ValueError("receipt set disagrees with inference policy")
    if (
        receipts.request_budget > policy.max_requests
        or receipts.prompt_token_budget > policy.max_prompt_tokens
        or receipts.completion_token_budget > policy.max_completion_tokens
    ):
        raise ValueError("receipt-set task budget exceeds inference policy")
    if len(receipts.receipts) > policy.max_requests + policy.max_retries:
        raise ValueError("receipt set exceeds the event budget")
    for receipt in receipts.receipts:
        if (
            receipt.attempt > policy.max_attempts_per_request
            or receipt.prompt_sha256 != policy.prompt_sha256
            or receipt.tool_schema_sha256 != policy.tool_schema_sha256
            or receipt.model != policy.model
            or receipt.provider_route != policy.provider_route
            or receipt.provider_route_profile != policy.provider_route_profile
            or receipt.fallback_used != policy.allow_fallbacks
            or receipt.receipt_provider not in {None, policy.receipt_provider}
            or receipt.completion_tokens > policy.max_completion_tokens_per_request
        ):
            raise ValueError("receipt identity disagrees with inference policy")
    terminal = [
        receipt
        for receipt in receipts.receipts
        if receipt.outcome is not CodingInferenceReceiptOutcome.RECEIPT_FREE_RETRY
    ]
    retries = len(receipts.receipts) - len(terminal)
    prompt = sum(receipt.prompt_tokens for receipt in receipts.receipts)
    completion = sum(receipt.completion_tokens for receipt in receipts.receipts)
    total = sum(receipt.total_tokens for receipt in receipts.receipts)
    cost = sum(receipt.cost_usd_micros for receipt in receipts.receipts)
    if (
        len(terminal) > receipts.request_budget
        or prompt > receipts.prompt_token_budget
        or completion > receipts.completion_token_budget
        or total > policy.max_total_tokens
        or cost > policy.max_cost_usd_micros
        or retries > policy.max_retries
    ):
        raise ValueError("coding inference receipt set exceeds policy budgets")
    return terminal, retries, prompt, completion, total, cost


def receipt_set_digest(
    receipts: CodingInferenceReceiptSet,
    policy: CodingInferencePolicy,
) -> str:
    policy = CodingInferencePolicy.model_validate_json(
        policy.model_dump_json(by_alias=True)
    )
    receipts = CodingInferenceReceiptSet.model_validate_json(
        receipts.model_dump_json(by_alias=True)
    )
    _receipt_policy_aggregate(policy, receipts)
    return coding_inference_digest(receipts)


def provider_settlement_digest(
    settlement: CodingInferenceProviderSettlement,
    policy: CodingInferencePolicy,
) -> str:
    policy = CodingInferencePolicy.model_validate_json(
        policy.model_dump_json(by_alias=True)
    )
    settlement = CodingInferenceProviderSettlement.model_validate_json(
        settlement.model_dump_json(by_alias=True)
    )
    selected = settlement.router_attempts[0].selected
    if (
        settlement.inference_grant_sha256 != policy_digest(policy)
        or settlement.request_sequence > policy.max_requests
        or settlement.attempt > policy.max_attempts_per_request
        or settlement.model != policy.model
        or settlement.provider_api != policy.provider_api
        or settlement.provider_route != policy.provider_route
        or settlement.provider_route_profile != policy.provider_route_profile
        or settlement.provider_account_guardrail != policy.provider_account_guardrail
        or settlement.provider_pipeline_policy != policy.provider_pipeline_policy
        or settlement.provider_cache_policy != policy.provider_cache_policy
        or settlement.router_metadata_verified != policy.router_metadata_required
        or settlement.router_attempts[0].provider != policy.receipt_provider
        or settlement.fallback_used
        or settlement.pipeline_stages
        or settlement.completion_tokens > policy.max_completion_tokens_per_request
        or settlement.prompt_tokens > policy.max_prompt_tokens
        or settlement.total_tokens > policy.max_total_tokens
        or settlement.cost_usd_micros > policy.max_cost_usd_micros
    ):
        raise ValueError("provider settlement disagrees with inference policy")
    if settlement.response_sha256 is None:
        digest_shape = settlement.response_digest_kind == "none"
    elif settlement.outcome is CodingInferenceReceiptOutcome.COMPLETE:
        digest_shape = settlement.response_digest_kind == "normalized_v1"
    else:
        digest_shape = settlement.response_digest_kind == "canonical_json_v1"
    if settlement.outcome is CodingInferenceReceiptOutcome.RECEIPT_FREE_RETRY:
        outcome_shape = (
            settlement.terminal_error_code == "pre_provider_unavailable"
            and settlement.http_status in {404, 408, 429, 500, 502, 503, 504}
            and settlement.response_sha256 is None
            and settlement.provider_generation_id is None
            and settlement.receipt_provider is None
            and not selected
            and not settlement.usage_available
            and not settlement.cost_available
            and not settlement.timed_out
        )
    elif settlement.outcome is CodingInferenceReceiptOutcome.COMPLETE:
        outcome_shape = (
            settlement.terminal_error_code is None
            and 200 <= settlement.http_status < 300
            and settlement.response_sha256 is not None
            and settlement.provider_generation_id is not None
            and settlement.receipt_provider == policy.receipt_provider
            and selected
            and settlement.usage_available
            and settlement.cost_available
            and not settlement.timed_out
        )
    else:
        failure_shape = {
            "pre_provider_unavailable": (
                not selected
                and settlement.http_status in {404, 408, 429, 500, 502, 503, 504}
                and settlement.response_sha256 is None
                and not settlement.timed_out
            ),
            "provider_timeout": (
                settlement.timed_out and settlement.http_status in {0, 408, 504}
            ),
            "provider_transport": (
                not settlement.timed_out
                and settlement.http_status == 0
                and settlement.response_sha256 is None
            ),
            "provider_http": (
                not settlement.timed_out and settlement.http_status >= 400
            ),
            "provider_response_invalid": (
                not settlement.timed_out
                and 200 <= settlement.http_status < 300
                and settlement.response_sha256 is not None
            ),
        }.get(settlement.terminal_error_code or "", False)
        provider_shape = (
            selected
            and settlement.receipt_provider == policy.receipt_provider
            and settlement.usage_available
            and settlement.cost_available
        ) or (
            not selected
            and settlement.receipt_provider is None
            and settlement.provider_generation_id is None
            and not settlement.usage_available
            and not settlement.cost_available
        )
        outcome_shape = failure_shape and provider_shape
    if not digest_shape or not outcome_shape:
        raise ValueError("provider settlement outcome is invalid")
    return coding_inference_digest(settlement)


def validate_settlement_against_receipt(
    settlement: CodingInferenceProviderSettlement,
    receipt: CodingInferenceReceipt,
    policy: CodingInferencePolicy,
) -> None:
    if (
        settlement.request_id != receipt.request_id
        or settlement.request_sequence != receipt.request_sequence
        or settlement.attempt != receipt.attempt
        or settlement.locked_request_sha256 != receipt.locked_request_sha256
        or settlement.outcome != receipt.outcome
        or settlement.terminal_error_code != receipt.failure_code
        or settlement.http_status != receipt.http_status
        or settlement.response_sha256 != receipt.response_sha256
        or settlement.response_digest_kind != receipt.response_digest_kind
        or settlement.provider_generation_id != receipt.provider_generation_id
        or settlement.router_attempts[0].selected != receipt.provider_selected
        or settlement.receipt_provider != receipt.receipt_provider
        or settlement.fallback_used != receipt.fallback_used
        or settlement.prompt_tokens != receipt.prompt_tokens
        or settlement.completion_tokens != receipt.completion_tokens
        or settlement.total_tokens != receipt.total_tokens
        or settlement.cost_usd_micros != receipt.cost_usd_micros
        or settlement.timed_out != receipt.timed_out
        or provider_settlement_digest(settlement, policy)
        != receipt.provider_settlement_sha256
    ):
        raise ValueError("receipt disagrees with provider settlement")


def derive_model_evidence(
    policy: CodingInferencePolicy,
    binding: CodingInferenceReceiptBinding,
    receipts: CodingInferenceReceiptSet | None,
    settlements: list[CodingInferenceProviderSettlement] | None = None,
) -> CodingInferenceModelEvidence:
    policy = CodingInferencePolicy.model_validate_json(
        policy.model_dump_json(by_alias=True)
    )
    binding = CodingInferenceReceiptBinding.model_validate_json(
        binding.model_dump_json(by_alias=True)
    )
    if receipts is not None:
        receipts = CodingInferenceReceiptSet.model_validate_json(
            receipts.model_dump_json(by_alias=True)
        )
    grant_sha256 = policy_digest(policy)
    if (
        binding.inference_grant_sha256 != grant_sha256
        or binding.request_budget > policy.max_requests
        or binding.prompt_token_budget > policy.max_prompt_tokens
        or binding.completion_token_budget > policy.max_completion_tokens
    ):
        raise ValueError("trusted receipt binding disagrees with inference policy")
    if receipts is None:
        if settlements:
            raise ValueError("not-invoked evidence cannot contain settlements")
        return CodingInferenceModelEvidence(
            model=policy.model,
            provider=policy.provider_route,
            provider_route_profile=policy.provider_route_profile,
            reasoning_effort=policy.reasoning_effort,
            inference_grant_sha256=grant_sha256,
            prompt_sha256=policy.prompt_sha256,
            tool_schema_sha256=policy.tool_schema_sha256,
            usage_status=CodingInferenceModelUsageStatus.NOT_INVOKED,
            fallback_used=False,
            cost_source=policy.cost_source,
            currency=policy.currency,
            provider_receipt_set_sha256=None,
            requests=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost_usd_micros=0,
            retry_count=0,
        )
    if (
        receipts.ticket_id != binding.ticket_id
        or receipts.case_id != binding.case_id
        or receipts.profile_capability_id != binding.profile_capability_id
        or receipts.grant_id != binding.grant_id
        or receipts.generation != binding.generation
        or receipts.inference_grant_sha256 != binding.inference_grant_sha256
        or receipts.request_budget != binding.request_budget
        or receipts.prompt_token_budget != binding.prompt_token_budget
        or receipts.completion_token_budget != binding.completion_token_budget
    ):
        raise ValueError("receipt set disagrees with trusted lease/grant binding")
    if settlements is None or len(settlements) != len(receipts.receipts):
        raise ValueError("provider settlement coverage is incomplete")
    settlements = [
        CodingInferenceProviderSettlement.model_validate_json(
            settlement.model_dump_json(by_alias=True)
        )
        for settlement in settlements
    ]
    for receipt, settlement in zip(receipts.receipts, settlements, strict=True):
        if (
            settlement.ticket_id != binding.ticket_id
            or settlement.case_id != binding.case_id
            or settlement.profile_capability_id != binding.profile_capability_id
            or settlement.inference_grant_sha256 != binding.inference_grant_sha256
            or settlement.grant_id != binding.grant_id
            or settlement.generation != binding.generation
        ):
            raise ValueError("provider settlement binding disagrees")
        validate_settlement_against_receipt(settlement, receipt, policy)
    terminal, retries, prompt, completion, total, cost = _receipt_policy_aggregate(
        policy, receipts
    )
    usage_status = (
        CodingInferenceModelUsageStatus.PROVIDER_FAILURE
        if terminal[-1].outcome is CodingInferenceReceiptOutcome.PROVIDER_FAILURE
        else CodingInferenceModelUsageStatus.COMPLETE
    )
    evidence = CodingInferenceModelEvidence(
        model=policy.model,
        provider=policy.provider_route,
        provider_route_profile=policy.provider_route_profile,
        reasoning_effort=policy.reasoning_effort,
        inference_grant_sha256=grant_sha256,
        prompt_sha256=policy.prompt_sha256,
        tool_schema_sha256=policy.tool_schema_sha256,
        usage_status=usage_status,
        fallback_used=False,
        cost_source=policy.cost_source,
        currency=policy.currency,
        provider_receipt_set_sha256=receipt_set_digest(receipts, policy),
        requests=len(terminal),
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_usd_micros=cost,
        retry_count=retries,
    )
    return evidence


def model_evidence_digest(
    evidence: CodingInferenceModelEvidence,
    policy: CodingInferencePolicy,
) -> str:
    """Hash the standalone relay vector; task evidence remains signing authority."""

    policy = CodingInferencePolicy.model_validate_json(
        policy.model_dump_json(by_alias=True)
    )
    evidence = CodingInferenceModelEvidence.model_validate_json(
        evidence.model_dump_json(by_alias=True)
    )
    if (
        evidence.model != policy.model
        or evidence.provider != policy.provider_route
        or evidence.provider_route_profile != policy.provider_route_profile
        or evidence.reasoning_effort != policy.reasoning_effort
        or evidence.inference_grant_sha256 != policy_digest(policy)
        or evidence.prompt_sha256 != policy.prompt_sha256
        or evidence.tool_schema_sha256 != policy.tool_schema_sha256
        or evidence.fallback_used != policy.allow_fallbacks
        or evidence.cost_source != policy.cost_source
        or evidence.currency != policy.currency
    ):
        raise ValueError("model evidence disagrees with inference policy")
    if (
        evidence.requests > policy.max_requests
        or evidence.prompt_tokens > policy.max_prompt_tokens
        or evidence.completion_tokens > policy.max_completion_tokens
        or evidence.total_tokens > policy.max_total_tokens
        or evidence.cost_usd_micros > policy.max_cost_usd_micros
        or evidence.retry_count > policy.max_retries
    ):
        raise ValueError("model evidence exceeds inference policy")
    return coding_inference_digest(evidence)


__all__ = [
    "CodingInferenceLockedRequest",
    "CodingInferenceModelEvidence",
    "CodingInferenceNormalizedResponse",
    "CodingInferenceProviderResponse",
    "CodingInferenceProviderSettlement",
    "CodingInferenceReceiptBinding",
    "CodingInferencePolicy",
    "CodingInferenceReceiptSet",
    "CodingInferenceSystemPrompt",
    "CodingInferenceToolSchema",
    "derive_model_evidence",
    "effective_inference_request_budget",
    "locked_request_digest",
    "model_evidence_digest",
    "normalize_provider_response",
    "normalized_response_digest",
    "parse_coding_inference_json",
    "policy_digest",
    "provider_settlement_digest",
    "receipt_set_digest",
    "system_prompt_digest",
    "tool_schema_digest",
    "validate_settlement_against_receipt",
]
