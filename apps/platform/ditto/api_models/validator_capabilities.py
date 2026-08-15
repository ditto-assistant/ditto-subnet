"""Fixed, signed validator execution and stack identity for heartbeat v7."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_REVISION_PATTERN = r"^[0-9a-f]{40}$"
_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+/-]{0,63}$"
_PROFILE_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+/-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

ComponentProvenance = Literal["signed_descriptor", "committed_pin", "local_unverified"]
ExecutorIsolation = Literal[
    "unknown", "privileged_dind", "rootless_dind", "rootless_host", "ephemeral_vm"
]
StackMode = Literal["source", "managed"]
ScorerBenchmarkStatus = Literal[
    "fresh_verified", "legacy_v2", "unreachable", "identity_mismatch"
]
SupportedBenchVersions = Annotated[
    tuple[Annotated[int, Field(ge=1)], ...],
    BeforeValidator(lambda value: tuple(value) if isinstance(value, list) else value),
]

# What the validator's ``/v1/capabilities`` probe actually did, as opposed to
# what the validator concluded from it. ``ScorerBenchmarkStatus`` is the
# conclusion; two very different scorers reach the same conclusion.
#
# ``served``           a capability document this validator read in full.
# ``served_degraded``  a readable document, part of which was rejected -- so the
#                      advertisement is narrower than what the scorer offered.
# ``http_error``       the scorer answered with a status this validator cannot
#                      use. ``http_status`` says which (404 on a sidecar that
#                      never mounted the route, 401 on a mis-scoped token).
# ``unreadable``       HTTP 200 whose body is not a usable capability document.
# ``timeout``          no answer inside the probe deadline.
# ``connect_error``    the transport never completed: refused, reset, DNS, TLS.
# ``not_probed``       no observation was attempted (mock/plumbing mode). Never
#                      evidence of health -- evidence that there is no evidence.
ScorerProbeOutcome = Literal[
    "served",
    "served_degraded",
    "http_error",
    "unreadable",
    "timeout",
    "connect_error",
    "not_probed",
]

# Why a readable answer was still not fully usable. Present only for the two
# outcomes that read a body and rejected some of it.
ScorerProbeReason = Literal[
    "invalid_json",
    "malformed_capabilities",
    "unsupported_bench_version",
    "calibration_unreadable",
    "identity_mismatch",
]

_PROBE_SERVING = frozenset({"served", "served_degraded"})
_PROBE_ANSWERED = frozenset({"served", "served_degraded", "http_error", "unreadable"})
_PROBE_READ_A_BODY = frozenset({"served", "served_degraded", "unreadable"})
_PROBE_REASONED = frozenset({"served_degraded", "unreadable"})


class ScorerLivenessProbe(BaseModel):
    """Evidence that the scorer is *serving*, not merely running.

    ``ScorerBenchmarkCapability`` reports the conclusion the validator drew from
    its ``/v1/capabilities`` probe. This reports the observation behind it.
    Without it the two failures that matter are indistinguishable on the wire,
    because both leave every identity field null: a scorer whose sidecar 404s
    the route entirely, and a scorer that answered with a document this
    validator could not use. Both collapse to ``legacy_v2``/``unreachable`` with
    nulls, so an operator cannot tell a dead sidecar from a shape drift.

    ``consecutive_failures`` and ``last_served_at`` are counted by the reporting
    process, so they restart with it. That is the honest reading: after a
    restart the validator has no evidence about the past, and reports none.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    outcome: ScorerProbeOutcome
    observed_at: Annotated[int, Field(ge=0)]
    http_status: Annotated[int | None, Field(ge=100, le=599)] = None
    reason: ScorerProbeReason | None = None
    last_served_at: Annotated[int | None, Field(ge=0)] = None
    consecutive_failures: Annotated[int, Field(ge=0, le=2**31 - 1)] = 0

    @model_validator(mode="after")
    def evidence_matches_outcome(self) -> ScorerLivenessProbe:
        if (self.outcome in _PROBE_ANSWERED) != (self.http_status is not None):
            raise ValueError(
                "a scorer probe reports an HTTP status exactly when it got an answer"
            )
        if self.outcome in _PROBE_READ_A_BODY and self.http_status != 200:
            raise ValueError("a scorer probe that read a body must have received 200")
        if self.outcome == "http_error" and self.http_status == 200:
            raise ValueError("HTTP 200 is not an HTTP error")
        if (self.outcome in _PROBE_REASONED) != (self.reason is not None):
            raise ValueError(
                "a degraded or unreadable scorer probe must name its reason"
            )
        if self.last_served_at is not None and self.last_served_at > self.observed_at:
            raise ValueError("the scorer cannot have served after it was observed")
        if self.outcome == "served":
            # The point of the whole field: "it served" must be unable to
            # coexist with evidence that it did not.
            if self.consecutive_failures:
                raise ValueError("a serving scorer has no outstanding probe failures")
            if self.last_served_at != self.observed_at:
                raise ValueError("a serving probe last served at its own observation")
        return self


class InferenceCalibrationRoute(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    provider: Annotated[str, Field(min_length=1, max_length=120)]
    profile_revision: Annotated[str, Field(pattern=_PROFILE_PATTERN)]
    model: Annotated[str, Field(min_length=1, max_length=120)]


InferenceCalibrationRoutes = Annotated[
    tuple[InferenceCalibrationRoute, ...],
    BeforeValidator(lambda value: tuple(value) if isinstance(value, list) else value),
]


class V7InferenceCalibration(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    manifest_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    supported_routes: InferenceCalibrationRoutes

    @model_validator(mode="after")
    def routes_are_nonempty_unique_and_sorted(self) -> V7InferenceCalibration:
        identities = tuple(
            (route.provider, route.profile_revision, route.model)
            for route in self.supported_routes
        )
        if not identities or tuple(sorted(set(identities))) != identities:
            raise ValueError("v7 inference routes must be nonempty, unique, and sorted")
        return self


class ScorerBenchmarkCapability(BaseModel):
    """Identity-bound benchmark support observed from the scorer sidecar."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    status: ScorerBenchmarkStatus
    supported_bench_versions: SupportedBenchVersions
    observed_at: Annotated[int | None, Field(ge=0)] = None
    software_version: Annotated[str | None, Field(pattern=_VERSION_PATTERN)] = None
    source_revision: Annotated[str | None, Field(pattern=_REVISION_PATTERN)] = None
    v7_calibration: V7InferenceCalibration | None = None
    # Additive and optional so a validator that predates heartbeat protocol v15
    # produces the exact same signing bytes it always did. Absent means "this
    # validator cannot report probe evidence", never "the probe succeeded".
    probe: ScorerLivenessProbe | None = None

    @model_validator(mode="after")
    def support_matches_verified_identity(self) -> ScorerBenchmarkCapability:
        versions = self.supported_bench_versions
        if tuple(sorted(set(versions))) != versions:
            raise ValueError("supported benchmark versions must be unique and sorted")
        if self.status == "fresh_verified":
            if (
                not versions
                or self.observed_at is None
                or self.software_version is None
                or self.source_revision is None
            ):
                raise ValueError(
                    "fresh verified scorer support requires observation and identity"
                )
        elif versions not in ((), (2,)):
            raise ValueError(
                "unverified scorer states may advertise no work or legacy benchmark v2"
            )
        # Any post-v2 benchmark requires a fresh verified identity. Keyed on the
        # RANGE, not on a specific version: the old `3 in versions` form meant a
        # scorer advertising a newer set that happened to omit 3 -- (2, 4), say --
        # skipped the check entirely.
        if any(version > 2 for version in versions) and self.status != "fresh_verified":
            raise ValueError(
                "benchmark versions above v2 require a fresh verified scorer identity"
            )
        # Heartbeat protocol v10 predates the v7 calibration payload. A source
        # stack may observe a newer scorer that advertises v7 while the
        # validator still emits that legacy envelope, so the protocol-aware
        # requirement belongs to ValidatorHeartbeatRequest. Keep rejecting the
        # inverse contradiction at this protocol-agnostic layer.
        if self.v7_calibration is not None and 7 not in versions:
            raise ValueError("v7 inference calibration requires benchmark v7 support")
        # When probe evidence is present it must agree with the conclusion. A
        # green scorer status that no probe actually observed is the exact shape
        # of both outages this field exists to make impossible.
        if self.probe is not None:
            if (
                self.status == "fresh_verified"
                and self.probe.outcome not in _PROBE_SERVING
            ):
                raise ValueError("a fresh verified scorer must have served its probe")
            if self.status == "identity_mismatch" and (
                self.probe.reason != "identity_mismatch"
            ):
                raise ValueError("an identity mismatch must be named by the probe")
            if self.status == "unreachable" and self.probe.outcome == "served":
                raise ValueError("an unreachable scorer cannot have served its probe")
        return self


class ValidatorCapabilities(BaseModel):
    """Closed capability set used by the platform to issue compatible work."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    screened_images: bool
    require_screened_image: bool
    source_build_fallback: bool
    full_stack_managed: bool
    stack_updater: bool
    sandbox_egress_restricted: bool
    ticket_inference: bool = Field(default=False, exclude_if=lambda value: not value)
    signed_score_quorum: bool = Field(default=False, exclude_if=lambda value: not value)
    executor_isolation: ExecutorIsolation
    scorer_benchmarks: ScorerBenchmarkCapability | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def screened_image_flags_are_consistent(self) -> ValidatorCapabilities:
        if self.require_screened_image and not self.screened_images:
            raise ValueError(
                "requiring screened images requires screened image support"
            )
        if self.require_screened_image == self.source_build_fallback:
            raise ValueError(
                "require_screened_image and source_build_fallback must be opposites"
            )
        if self.stack_updater and not self.full_stack_managed:
            raise ValueError("stack updater requires a managed full stack")
        if (
            self.scorer_benchmarks is not None
            and self.scorer_benchmarks.v7_calibration is not None
            and not self.ticket_inference
        ):
            raise ValueError("v7 calibration requires ticket inference")
        return self


class ValidatorComponentIdentity(BaseModel):
    """Bounded identity for one member of the validator Compose stack."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    image_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)] = None
    source_revision: Annotated[str | None, Field(pattern=_REVISION_PATTERN)] = None
    version: Annotated[str | None, Field(pattern=_VERSION_PATTERN)] = None
    provenance: ComponentProvenance

    @model_validator(mode="after")
    def has_identity(self) -> ValidatorComponentIdentity:
        if (
            self.image_digest is None
            and self.source_revision is None
            and self.version is None
        ):
            raise ValueError(
                "component must provide an image, source, or version identity"
            )
        if self.provenance == "signed_descriptor" and self.image_digest is None:
            raise ValueError("signed descriptor components require an image digest")
        return self


class ValidatorStackComponents(BaseModel):
    """Current stack components plus nullable rolling-transition sidecars."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    ditto_subnet: ValidatorComponentIdentity
    dittobench_api: ValidatorComponentIdentity
    sandbox_docker: ValidatorComponentIdentity
    model_relay: ValidatorComponentIdentity | None = None
    pylon: ValidatorComponentIdentity
    ollama: ValidatorComponentIdentity | None = None


class ValidatorStackIdentity(BaseModel):
    """Verified release identity, or explicit source-stack identity."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: StackMode
    compose_schema: Annotated[int, Field(ge=1, le=2**31 - 1)]
    release_descriptor_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)] = (
        None
    )
    components: ValidatorStackComponents

    @model_validator(mode="after")
    def mode_matches_provenance(self) -> ValidatorStackIdentity:
        identities = tuple(
            item for item in self.components.__dict__.values() if item is not None
        )
        if self.mode == "managed":
            if self.release_descriptor_digest is None:
                raise ValueError("managed stacks require a release descriptor digest")
            if any(item.provenance != "signed_descriptor" for item in identities):
                raise ValueError(
                    "managed stack components must come from signed descriptor"
                )
        else:
            if self.release_descriptor_digest is not None:
                raise ValueError("source stacks cannot claim a release descriptor")
            if any(item.provenance == "signed_descriptor" for item in identities):
                raise ValueError(
                    "source stack components cannot claim signed provenance"
                )
        return self


def validator_identity_signing_token(
    capabilities: ValidatorCapabilities, stack: ValidatorStackIdentity
) -> str:
    """Return one length-prefixed canonical JSON token for heartbeat v7."""
    payload = json.dumps(
        {
            # Recursive omission on capabilities preserves the exact legacy
            # heartbeat-v7 token when the additive scorer capability is absent.
            "capabilities": capabilities.model_dump(mode="json", exclude_none=True),
            # Stack nulls were part of the original v7 canonical fixture.
            "stack": stack.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{len(payload.encode('utf-8'))}:{payload}"


def validator_artifact_mode(
    capabilities: ValidatorCapabilities,
) -> Literal["legacy", "prefer_screened", "screened_only"]:
    if capabilities.require_screened_image:
        return "screened_only"
    if capabilities.screened_images:
        return "prefer_screened"
    return "legacy"
