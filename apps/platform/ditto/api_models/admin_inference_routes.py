"""Wire contract for the operator view of inference route admission.

Declared as models rather than served from a ``dict[str, object]`` handler for
one specific reason: ``provider_telemetry`` is built from ``func.sum`` and
``func.avg``, which Postgres types as ``numeric`` and asyncpg hands back as
``Decimal``. Serialized through an untyped ``object`` annotation, Pydantic v2
renders ``Decimal`` as a JSON *string*, so this endpoint served
``"prompt_tokens": "80"`` and ``"average_latency_ms":
"250.0000000000000000"`` in production. The integer and float fields here are
what pin those back to JSON numbers, and what will keep any future aggregate
column from silently flipping shape again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AggregateRouteView(BaseModel):
    """The single logical route served while routing mode is aggregate."""

    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str
    profile_revision: str
    provider_sort: str
    provider_order: list[str]
    reliability_provider_order: list[str]
    ignored_providers: list[str]
    allow_fallbacks: bool


class InferenceRoutingPolicyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    revision: int = Field(ge=0)
    enabled: bool
    speed_weight: float
    cost_weight: float
    exploration_weight: float
    exploration_ticket_budget: int
    min_tool_accuracy: float
    min_composite: float
    min_calibration_samples: int
    max_error_rate: float
    max_timeout_rate: float
    cooldown_seconds: int
    ewma_alpha: float
    updated_at: datetime


class InferenceRouteView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str
    profile_revision: str
    quantization: str | None
    status: str
    calibration_status: str
    calibration_revision: int = Field(ge=0)
    calibration_manifest_sha256: str | None
    calibration_sample_count: int = Field(ge=0)
    calibration_tool_accuracy: float | None
    calibration_composite: float | None
    sample_count: int = Field(ge=0)
    selected_ticket_count: int = Field(ge=0)
    exploration_ticket_count: int = Field(ge=0)
    last_selected_at: datetime | None
    ewma_tokens_per_second: float | None
    ewma_latency_ms: float | None
    ewma_error_rate: float
    ewma_timeout_rate: float
    prompt_price_per_token: float | None
    completion_price_per_token: float | None
    updated_at: datetime


class InferenceRoutingAuditView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    actor: str
    action: str
    model: str
    profile_revision: str | None
    payload: dict[str, Any]
    recorded_at: datetime


class ProviderTelemetryView(BaseModel):
    """Per-upstream chat totals aggregated from ``inference_requests``.

    Every count here is an integer on the wire. ``average_latency_ms`` is a
    float, and ``None`` when a provider's sampled requests all recorded a null
    latency -- ``latency_ms`` is nullable, so ``avg()`` over an all-null group
    is ``NULL``, not zero. Reporting zero would read as "instant" rather than
    "not measured".
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    request_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    inflight_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    upstream_attempt_count: int = Field(ge=0)
    openrouter_attempt_count: int = Field(ge=0)
    recovered_after_fallback_count: int = Field(ge=0)
    terminal_failure_count: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_microusd: int = Field(ge=0)
    average_latency_ms: float | None
    observed_output_tps: float | None


class RelayRecoveryTelemetryView(BaseModel):
    """Ticket-level abort evidence retained by the benchmark control plane."""

    model_config = ConfigDict(extra="forbid")

    benchmark_relay_abort_ticket_count: int = Field(ge=0)
    broker_recovery_exhausted_ticket_count: int = Field(ge=0)


class AdminInferenceRoutes(BaseModel):
    """Everything the operator console renders for inference routing."""

    model_config = ConfigDict(extra="forbid")

    routing_mode: str
    aggregate_route: AggregateRouteView | None
    policies: list[InferenceRoutingPolicyView]
    routes: list[InferenceRouteView]
    audits: list[InferenceRoutingAuditView]
    provider_telemetry: list[ProviderTelemetryView]
    relay_recovery_telemetry: RelayRecoveryTelemetryView
