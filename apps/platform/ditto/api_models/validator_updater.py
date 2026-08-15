"""Privacy-safe managed validator updater telemetry for heartbeat protocol v23."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_DESCRIPTOR_PATTERN = (
    r"^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$"
)
_VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"

ValidatorUpdaterPhase = Literal[
    "prepared",
    "drained",
    "old_stopped",
    "candidate_started",
    "committed",
    "rollback_pending",
    "rollback_ready",
]
ValidatorUpdaterState = Literal[
    "not_managed",
    "disabled",
    "unavailable",
    "idle",
    "prefetched",
    "draining",
    "replacing",
    "verifying",
    "rollback",
    "backoff",
    "retry_ready",
    "suppressed",
]
ValidatorUpdaterFailureReason = Literal[
    "candidate_deploy_failed",
    "candidate_readiness_failed",
    "transaction_interrupted",
    "unknown",
]


class ValidatorUpdaterStatus(BaseModel):
    """Bounded updater state; never contains paths, logs, env, or errors."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool
    channel: Literal["compat-2"] | None = None
    state: ValidatorUpdaterState
    transaction_phase: ValidatorUpdaterPhase | None = None
    current_descriptor: Annotated[str | None, Field(pattern=_DESCRIPTOR_PATTERN)] = None
    current_version: Annotated[str | None, Field(pattern=_VERSION_PATTERN)] = None
    candidate_descriptor: Annotated[str | None, Field(pattern=_DESCRIPTOR_PATTERN)] = (
        None
    )
    candidate_version: Annotated[str | None, Field(pattern=_VERSION_PATTERN)] = None
    failed_candidate_count: Annotated[int, Field(ge=0, le=100)] = 0
    retry_after: Annotated[int | None, Field(ge=0)] = None
    suppressed: bool = False
    last_success_at: Annotated[int | None, Field(ge=0)] = None
    last_failure_at: Annotated[int | None, Field(ge=0)] = None
    last_failure_reason: ValidatorUpdaterFailureReason | None = None
    observed_at: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_mode(self) -> ValidatorUpdaterStatus:
        if self.state == "not_managed":
            if self.enabled or self.channel is not None:
                raise ValueError("not_managed telemetry cannot enable a channel")
            if (
                any(
                    value is not None
                    for value in (
                        self.transaction_phase,
                        self.current_descriptor,
                        self.current_version,
                        self.candidate_descriptor,
                        self.candidate_version,
                        self.retry_after,
                        self.last_success_at,
                        self.last_failure_at,
                        self.last_failure_reason,
                    )
                )
                or self.failed_candidate_count
                or self.suppressed
            ):
                raise ValueError("not_managed telemetry cannot claim updater state")
            return self
        if not self.enabled:
            if self.state != "disabled" or self.channel != "compat-2":
                raise ValueError("disabled managed telemetry must retain its channel")
            if (
                self.transaction_phase is not None
                or self.candidate_descriptor is not None
                or self.candidate_version is not None
                or self.failed_candidate_count
                or self.retry_after is not None
                or self.suppressed
                or self.last_failure_at is not None
                or self.last_failure_reason is not None
            ):
                raise ValueError("disabled updater telemetry cannot claim active work")
            return self
        if self.channel != "compat-2" or self.state == "not_managed":
            raise ValueError("enabled updater telemetry requires the managed channel")
        if self.suppressed != (self.state == "suppressed"):
            raise ValueError("suppressed flag and updater state must agree")
        if self.transaction_phase is not None and self.state in {
            "idle",
            "prefetched",
            "backoff",
            "retry_ready",
            "suppressed",
        }:
            raise ValueError("transaction phase contradicts updater state")
        if self.state in {"backoff", "retry_ready", "suppressed"} and (
            self.failed_candidate_count < 1
            or self.retry_after is None
            or self.candidate_descriptor is None
        ):
            raise ValueError("retry state requires bounded failed-candidate evidence")
        return self


def validator_updater_status_signing_token(status: ValidatorUpdaterStatus) -> str:
    """Return one length-prefixed canonical JSON heartbeat token."""

    encoded = json.dumps(
        status.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{len(encoded.encode())}:{encoded}"


__all__ = [
    "ValidatorUpdaterFailureReason",
    "ValidatorUpdaterPhase",
    "ValidatorUpdaterState",
    "ValidatorUpdaterStatus",
    "validator_updater_status_signing_token",
]
