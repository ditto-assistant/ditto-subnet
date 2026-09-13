"""Admin wire models for creating one hosted-v2 shadow assignment."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field

from ditto.api_models.coding_evaluation import CodingEvaluationModel, Sha256

ValidatorHotkey = Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")]


class AdminHostedAssignmentSubject(CodingEvaluationModel):
    """Operator-chosen inputs; artifact and release digests are derived server-side."""

    agent_id: UUID
    release_row_id: UUID
    catalog_index: Annotated[int, Field(strict=True, ge=0, le=249)]
    validator_hotkey: ValidatorHotkey
    policy_sha256: Sha256
    execution_profile_sha256: Sha256
    grading_profile_sha256: Sha256
    max_patch_bytes: Annotated[int, Field(strict=True, ge=1, le=128 << 20)]


class AdminHostedAssignmentPreviewRequest(AdminHostedAssignmentSubject):
    lease_seconds: Annotated[int, Field(strict=True, ge=60, le=3600)] = 900


class AdminHostedAssignmentCreateRequest(AdminHostedAssignmentSubject):
    evaluation_id: UUID
    attempt_id: UUID
    deadline_unix: Annotated[int, Field(strict=True, gt=0)]
    confirmed_assignment_sha256: Sha256
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminHostedAssignmentPlan(CodingEvaluationModel):
    evaluation_id: UUID
    attempt_id: UUID
    deadline_unix: int
    artifact_sha256: Sha256
    screened_image_sha256: Sha256
    registration_sha256: Sha256
    bench_version: int
    certification_row_id: UUID
    schedule_sha256: Sha256
    selection_sha256: Sha256
    selection: dict[str, Any]
    assignment_sha256: Sha256
    authority: dict[str, Any]
    confirmation: str
    shadow_only: Literal[True] = True
    weight_eligible: Literal[False] = False


class AdminHostedAssignmentCreated(AdminHostedAssignmentPlan):
    authoring_grant_id: UUID
    grading_grant_id: UUID
