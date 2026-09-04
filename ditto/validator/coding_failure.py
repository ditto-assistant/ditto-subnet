"""Fail-closed terminal-domain classification for shadow coding tasks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ditto.api_models.coding import (
    CodingAuthoringEvidence,
    CodingGraderEvidence,
    CodingModelUsageStatus,
    CodingRunManifest,
    CodingTaskEvidence,
    CodingTerminalDomain,
    task_evidence_digest,
)


class CodingFailureClassificationError(ValueError):
    """A failure stage or its evidence cannot produce canonical task evidence."""


class CodingFailureStage(StrEnum):
    POST_LEASE_TRANSPORT = "post_lease_transport"
    TASK_MATERIAL = "task_material"
    AUTHORING_INFRASTRUCTURE = "authoring_infrastructure"
    CANDIDATE_INTEGRITY = "candidate_integrity"
    GRADING_INFRASTRUCTURE = "grading_infrastructure"
    REPAIR_FAILURE = "repair_failure"
    CONTROL_PLANE_INTEGRITY = "control_plane_integrity"


class CodingFailureCode(StrEnum):
    POST_LEASE_TRANSPORT = "post_lease_transport"
    TASK_MATERIAL_INVALID = "task_material_invalid"
    AUTHORING_RUNTIME = "authoring_runtime"
    CANDIDATE_POLICY_VIOLATION = "candidate_policy_violation"
    GRADING_RUNTIME = "grading_runtime"
    GRADER_TESTS_FAILED = "grader_tests_failed"
    CONTROL_PLANE_MISMATCH = "control_plane_mismatch"


@dataclass(frozen=True)
class _FailurePolicy:
    terminal_domain: CodingTerminalDomain
    authoring: _EvidenceRequirement
    grader: _EvidenceRequirement
    failure_code: CodingFailureCode


class _EvidenceRequirement(StrEnum):
    FORBIDDEN = "forbidden"
    OPTIONAL = "optional"
    REQUIRED = "required"
    REQUIRED_GRADEABLE = "required_gradeable"
    OPTIONAL_FAILED = "optional_failed"
    REQUIRED_FAILED = "required_failed"


_POLICIES = {
    CodingFailureStage.POST_LEASE_TRANSPORT: _FailurePolicy(
        CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE,
        _EvidenceRequirement.FORBIDDEN,
        _EvidenceRequirement.FORBIDDEN,
        CodingFailureCode.POST_LEASE_TRANSPORT,
    ),
    CodingFailureStage.TASK_MATERIAL: _FailurePolicy(
        CodingTerminalDomain.TASK_INVALID,
        _EvidenceRequirement.FORBIDDEN,
        _EvidenceRequirement.FORBIDDEN,
        CodingFailureCode.TASK_MATERIAL_INVALID,
    ),
    CodingFailureStage.AUTHORING_INFRASTRUCTURE: _FailurePolicy(
        CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE,
        _EvidenceRequirement.FORBIDDEN,
        _EvidenceRequirement.FORBIDDEN,
        CodingFailureCode.AUTHORING_RUNTIME,
    ),
    CodingFailureStage.CANDIDATE_INTEGRITY: _FailurePolicy(
        CodingTerminalDomain.CANDIDATE_INTEGRITY,
        _EvidenceRequirement.REQUIRED,
        _EvidenceRequirement.OPTIONAL_FAILED,
        CodingFailureCode.CANDIDATE_POLICY_VIOLATION,
    ),
    CodingFailureStage.GRADING_INFRASTRUCTURE: _FailurePolicy(
        CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE,
        _EvidenceRequirement.REQUIRED_GRADEABLE,
        _EvidenceRequirement.FORBIDDEN,
        CodingFailureCode.GRADING_RUNTIME,
    ),
    CodingFailureStage.REPAIR_FAILURE: _FailurePolicy(
        CodingTerminalDomain.REPAIR_FAILURE,
        _EvidenceRequirement.REQUIRED_GRADEABLE,
        _EvidenceRequirement.REQUIRED_FAILED,
        CodingFailureCode.GRADER_TESTS_FAILED,
    ),
    CodingFailureStage.CONTROL_PLANE_INTEGRITY: _FailurePolicy(
        CodingTerminalDomain.CONTROL_PLANE_INTEGRITY,
        _EvidenceRequirement.OPTIONAL,
        _EvidenceRequirement.FORBIDDEN,
        CodingFailureCode.CONTROL_PLANE_MISMATCH,
    ),
}


def build_coding_failure_task_evidence(
    manifest: CodingRunManifest,
    *,
    validator_ticket_id: str,
    case_id: str,
    variant_id: str,
    stage: CodingFailureStage,
    failure_code: CodingFailureCode,
    authoring: CodingAuthoringEvidence | None = None,
    grader: CodingGraderEvidence | None = None,
) -> CodingTaskEvidence:
    """Map one typed failure stage to immutable task evidence and validate it."""

    try:
        manifest = CodingRunManifest.model_validate_json(
            manifest.model_dump_json(by_alias=True)
        )
        if not isinstance(stage, CodingFailureStage):
            raise ValueError("failure stage is not typed")
        policy = _POLICIES[stage]
        if (
            not isinstance(failure_code, CodingFailureCode)
            or failure_code is not policy.failure_code
        ):
            raise ValueError("failure code does not match typed stage")
        selected = [
            task
            for task in manifest.tasks
            if task.case_id == case_id and task.variant_id == variant_id
        ]
        if len(selected) != 1:
            raise ValueError("failure task identity is not selected exactly once")
        _validate_component_policy(
            policy,
            authoring=authoring,
            grader=grader,
        )
        evidence = CodingTaskEvidence(
            schema="dittobench-coding-task-evidence-v1",
            coding_contract_version=1,
            weight_eligible=False,
            coding_run_id=manifest.coding_run_id,
            validator_ticket_id=validator_ticket_id,
            agent_id=manifest.agent_id,
            agent_artifact_sha256=manifest.agent_artifact_sha256,
            corpus_release_id=manifest.corpus_release_id,
            task_set_id=manifest.task_set_id,
            task_set_manifest_sha256=manifest.task_set_manifest_sha256,
            task=selected[0],
            authoring=authoring,
            grader=grader,
            terminal_domain=policy.terminal_domain,
            failure_code=failure_code.value,
            repair_score_micros=0,
        )
        task_evidence_digest(manifest, validator_ticket_id, evidence)
        return evidence
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise CodingFailureClassificationError(
            "coding failure evidence disagrees with typed stage authority"
        ) from error


def _validate_component_policy(
    policy: _FailurePolicy,
    *,
    authoring: CodingAuthoringEvidence | None,
    grader: CodingGraderEvidence | None,
) -> None:
    if (
        policy.authoring
        in {
            _EvidenceRequirement.REQUIRED,
            _EvidenceRequirement.REQUIRED_GRADEABLE,
        }
        and authoring is None
    ):
        raise ValueError("failure stage requires authoring evidence")
    if policy.authoring is _EvidenceRequirement.FORBIDDEN and authoring is not None:
        raise ValueError("failure stage precedes authoritative authoring")
    if policy.grader is _EvidenceRequirement.FORBIDDEN and grader is not None:
        raise ValueError("failure stage cannot carry grader evidence")
    if policy.grader is _EvidenceRequirement.REQUIRED_FAILED and grader is None:
        raise ValueError("failure stage requires grader evidence")
    if policy.grader in {
        _EvidenceRequirement.REQUIRED_FAILED,
        _EvidenceRequirement.OPTIONAL_FAILED,
    } and (grader is not None and grader.resolved()):
        raise ValueError("failure stage cannot carry a resolved grader")
    if (
        authoring is not None
        and (
            policy.authoring is _EvidenceRequirement.REQUIRED_GRADEABLE
            or grader is not None
        )
        and (
            authoring.model.usage_status
            not in {
                CodingModelUsageStatus.COMPLETE,
                CodingModelUsageStatus.NOT_INVOKED,
                CodingModelUsageStatus.PROVIDER_FAILURE,
            }
            or not authoring.protected_paths_intact
            or authoring.changed_path_count < 0
        )
    ):
        raise ValueError("failure stage requires gradeable authoring evidence")
