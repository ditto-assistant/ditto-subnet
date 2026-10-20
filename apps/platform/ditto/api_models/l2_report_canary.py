"""Exact-attempt, non-authoritative L2 canary wire contract."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol import (
    ScoredRuntimeEvidenceLease,
    ScreenerReviewSettingsOverride,
)


class L2CanaryScheduleBase(BaseModel):
    """Fields shared by the plain and pinned schedule routes.

    No route takes this model directly, so it never appears in the API schema.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    # FastAPI parses JSON into Python strings before model validation. Keep the
    # rest of this wire model strict while accepting canonical UUID strings.
    request_id: Annotated[UUID, Field(strict=False)]
    agent_id: Annotated[UUID, Field(strict=False)]
    source_attempt_id: Annotated[UUID, Field(strict=False)]
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    policy_version: Literal[13]
    expected_agent_status: str
    expected_score_count: Annotated[int, Field(ge=0)]
    target_node_id: str
    review_label: Literal["candidate_clear", "known_reject"]
    run_mode: Literal["source_only", "full_runtime"] = "source_only"
    historical_ruling_kind: Literal["ath_clear", "screening_reject"] | None = None
    historical_ruling_id: Annotated[UUID | None, Field(strict=False)] = None
    confirm_report_only: Literal[True]

    @model_validator(mode="after")
    def historical_ruling_is_explicit_and_source_only(self) -> Self:
        if (self.historical_ruling_kind is None) != (self.historical_ruling_id is None):
            raise ValueError("historical ruling kind and id must be supplied together")
        if self.historical_ruling_kind is not None and (
            self.run_mode != "source_only"
            or (self.historical_ruling_kind == "ath_clear")
            != (self.review_label == "candidate_clear")
        ):
            raise ValueError("historical ruling must match source-only review label")
        return self


class L2CanaryScheduleRequest(L2CanaryScheduleBase):
    """The plain route: the canary runs under the claiming node's posture."""

    @model_validator(mode="before")
    @classmethod
    def pin_uses_the_pinned_route(cls, data: Any) -> Any:
        # Every other unknown key is ignored, but a pin must not be: dropping
        # it would queue the canary under the node's posture. Refuse the key
        # even when null, so a client learns the route before it matters.
        if isinstance(data, dict) and "review_settings_revision" in data:
            raise ValueError(
                "review_settings_revision is scheduled with POST "
                "/admin/screener-l2-report-canaries/pinned, not this route"
            )
        return data


class L2CanaryPinnedScheduleRequest(L2CanaryScheduleBase):
    """``POST /pinned``: the schedule request with a required posture pin."""

    # Run under this immutable ``l2-report-canary*`` revision instead of the
    # claiming worker's node-effective posture. Only this route accepts it, so
    # a Platform build that predates pins refuses the route instead of
    # ignoring the field.
    review_settings_revision: Annotated[int, Field(ge=1)]


class CanonicalFixtureRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    request_id: Annotated[UUID, Field(strict=False)]
    target_node_id: Annotated[str, Field(min_length=1, max_length=63)]
    confirm_report_only: Literal[True]


class CanonicalFixtureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    reviewer_evidence_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reviewed_archive_sha256: Literal[
        "2f14f77cc8e21b57e96f304f3b621d9919e9af802076928a27301d57aa956d7e"
    ]
    reviewed_dockerfile_sha256: Literal[
        "d3a1a2a1e5d43b0465c28712457d95432942ac8f017fd10d538859a901a54641"
    ]
    built_image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    reviewer_evidence_url: Annotated[
        str, Field(pattern=r"^https://github\.com/ditto-assistant/ditto-subnet/")
    ]
    confirm_candidate_review: Literal[True]


class CanonicalFixtureScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    confirm_report_only: Literal[True]


class L2CanaryView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    canary_id: UUID
    request_id: UUID
    agent_id: UUID | None
    source_attempt_id: UUID | None
    source_kind: Literal["submission", "canonical_starter_fixture"] = "submission"
    fixture_key: str | None = None
    artifact_sha256: str
    target_node_id: str
    expected_agent_status: str | None
    expected_score_count: int | None
    review_label: str
    run_mode: Literal["source_only", "full_runtime"]
    source_attestation: dict | None = None
    # Scheduled posture pin (all three or none) and the posture the claim bound.
    review_settings_revision: int | None = None
    review_settings_scope: str | None = None
    review_settings_checksum: str | None = None
    settings_revision: int | None = None
    settings_checksum: str | None = None
    status: str
    claimed_instance_id: str | None
    lease_expires_at: datetime | None
    report: dict | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


L2CanaryGuardName = Literal[
    "ath_clear_action",
    "attempt_owner",
    "agent_artifact_sha256",
    "attempt_policy_version",
    "agent_status",
    "score_row_count",
    "attempt_artifact_sha256",
    "historical_ruling_run_mode",
    "historical_ruling",
    "source_object_verified",
    "arrival_bench_version",
]


class L2CanaryGuardCheck(BaseModel):
    """One exact-source guard, in the order the scheduler evaluates it.

    ``passed`` is null when the caller supplied no expected value to compare,
    or, for ``source_object_verified``, because only scheduling re-hashes the
    stored object. ``conflict_detail`` is the exact 409 detail the scheduler
    answers when this is the first guard not known to pass.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    guard: L2CanaryGuardName
    passed: bool | None
    current: str | int | None
    expected: str | int | None
    conflict_detail: str
    note: str | None = None


class L2CanaryPreflightView(BaseModel):
    """The scheduler's exact-source guards on current state; advisory only.

    It authorizes nothing: scheduling reruns the same predicate under row locks
    and, for a historical ruling, re-hashes the stored object before queueing.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    authority: Literal["none"] = "none"
    agent_id: UUID
    source_attempt_id: UUID
    agent_artifact_sha256: str
    source_attempt_artifact_sha256: str | None
    agent_status: str
    attempt_policy_version: int
    # Null when unavailable; the arrival_bench_version guard then fails exactly
    # as scheduling refuses it.
    arrival_bench_version: int | None
    score_row_count: int
    attempt_agent_id: UUID
    # Legacy attempts predate artifact pinning; only a historical ruling can
    # schedule them, and the ruling does not prove what the old attempt ran.
    legacy_null_attempt_sha256: bool
    active_canary_id: UUID | None
    # Whether a claim could lease now: the same report-only scored-runtime
    # packet lookup the claim path performs. A queued canary waits otherwise.
    report_only_packet_available: bool
    guards: list[L2CanaryGuardCheck]
    # False when any guard failed; null when none failed but at least one
    # expected value was not supplied (or object verification is pending).
    guards_pass: bool | None


class L2CanaryClaimRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    instance_id: Annotated[str, Field(min_length=1, max_length=63)]
    settings_revision: Annotated[int, Field(ge=0)]
    settings_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    # A rolling older worker omits this and would ignore a pinned posture, so
    # Platform leases pinned canaries only to workers that declare support.
    accepts_review_settings_override: bool = False


class L2CanaryClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    canary_id: UUID
    agent_id: UUID
    source_attempt_id: UUID
    artifact_sha256: str
    bench_version: int
    policy_version: int
    run_mode: Literal["source_only", "full_runtime"] = "source_only"
    source_kind: Literal["submission", "canonical_starter_fixture"] = "submission"
    source_attestation: dict | None = None
    miner_hotkey: str
    lease_token: str
    lease_expires_at: datetime
    download_url: str
    scored_runtime_evidence: ScoredRuntimeEvidenceLease
    # The scheduled posture pin. The worker applies it to this canary's own
    # gate only; its primary gate and next production claim are unaffected.
    review_settings_override: ScreenerReviewSettingsOverride | None = None


class L2CanaryCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    lease_token: str
    status: Literal["succeeded", "incomplete"]
    report: dict
    error_code: Annotated[str, Field(min_length=1, max_length=120)] | None = None

    @model_validator(mode="after")
    def bounded_report(self) -> L2CanaryCompleteRequest:
        if len(json.dumps(self.report, separators=(",", ":"))) > 512_000:
            raise ValueError("canary report exceeds 512000 bytes")
        return self


class L2CanaryCompleteResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    accepted: bool
