"""Signed per-component validator stack runtime health for heartbeat v9.

Heartbeat v7/v8 report *configured* identity for the six Compose components
(:mod:`ditto.api_models.validator_capabilities`); this module adds the
*observed* runtime side: whether each component is currently reachable and
functionally ready, and what identity — if any — could be independently
observed from a live probe. The schema is deliberately closed and bounded:
health is a small enum, observations are Unix timestamps and booleans, and an
observed identity reuses the exact digest/revision/version grammar of the
configured identity. Anything host-shaped (container ids, hostnames, URLs,
paths, environment values) has no field to live in and is rejected by
``extra="forbid"``.

A probe that cannot independently observe a running digest/revision reports
``None`` (unknown) rather than echoing the configured pin into an observed
field — observed identity is evidence, never a copy of intent.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.validator_capabilities import (
    _DIGEST_PATTERN,
    _REVISION_PATTERN,
    _VERSION_PATTERN,
)

ComponentHealthState = Literal[
    "healthy", "degraded", "unreachable", "identity_mismatch", "unknown"
]


class ObservedComponentIdentity(BaseModel):
    """Identity actually observed from a live component, never a copied pin."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    image_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)] = None
    source_revision: Annotated[str | None, Field(pattern=_REVISION_PATTERN)] = None
    version: Annotated[str | None, Field(pattern=_VERSION_PATTERN)] = None

    @model_validator(mode="after")
    def has_observation(self) -> ObservedComponentIdentity:
        if (
            self.image_digest is None
            and self.source_revision is None
            and self.version is None
        ):
            raise ValueError(
                "an observed identity must contain at least one observed field"
            )
        return self


class ValidatorComponentHealth(BaseModel):
    """One bounded runtime-health observation for one Compose component.

    ``ready`` is endpoint/functional readiness (the probe exercised the
    component's API, not merely container liveness). ``model_ready`` is the
    component-specific functional capability — the required embedding model
    for ``ollama``, the required model route for ``model_relay`` — and stays
    ``None`` where it does not apply. ``observed_at`` is the probe time, so
    component-probe staleness is visible independently of heartbeat staleness.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    health: ComponentHealthState
    required: bool
    observed_at: Annotated[int | None, Field(ge=0)] = None
    ready: bool | None = None
    model_ready: bool | None = None
    observed_identity: ObservedComponentIdentity | None = None

    @model_validator(mode="after")
    def observations_match_health(self) -> ValidatorComponentHealth:
        if self.health == "unknown":
            if (
                self.observed_at is not None
                or self.ready is not None
                or self.model_ready is not None
                or self.observed_identity is not None
            ):
                raise ValueError("unknown component health cannot carry observations")
            return self
        if self.observed_at is None:
            raise ValueError("an observed component health requires observed_at")
        if self.health == "unreachable" and (
            self.ready is True
            or self.model_ready is True
            or self.observed_identity is not None
        ):
            raise ValueError(
                "an unreachable component cannot report readiness or identity"
            )
        if self.health == "identity_mismatch" and self.observed_identity is None:
            raise ValueError(
                "an identity mismatch requires the mismatching observed identity"
            )
        if self.health == "healthy" and self.ready is False:
            raise ValueError("a healthy component cannot report an unready endpoint")
        return self


class ValidatorStackHealth(BaseModel):
    """Exactly the six supported Compose components, best-effort observed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ditto_subnet: ValidatorComponentHealth
    dittobench_api: ValidatorComponentHealth
    sandbox_docker: ValidatorComponentHealth
    model_relay: ValidatorComponentHealth
    pylon: ValidatorComponentHealth
    ollama: ValidatorComponentHealth

    @model_validator(mode="after")
    def reporter_observes_itself(self) -> ValidatorStackHealth:
        # The validator process is the reporter: it is definitionally required
        # and cannot be unreachable or unobserved from its own heartbeat.
        if not self.ditto_subnet.required:
            raise ValueError("the reporting validator component is always required")
        if self.ditto_subnet.health in ("unreachable", "unknown"):
            raise ValueError(
                "the reporting validator cannot be unreachable or unknown to itself"
            )
        return self


def validator_stack_health_signing_token(health: ValidatorStackHealth) -> str:
    """Return one length-prefixed canonical JSON token for heartbeat v9.

    Mirrors :func:`ditto.api_models.validator_capabilities.
    validator_identity_signing_token`: sorted keys, no whitespace, ASCII, and
    ``exclude_none`` so absent observations never widen the signed bytes.
    """
    payload = json.dumps(
        health.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{len(payload.encode('utf-8'))}:{payload}"
