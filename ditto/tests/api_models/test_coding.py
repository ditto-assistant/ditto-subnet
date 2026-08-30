from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.coding import (
    CodingAuthoringEvidence,
    CodingAuthoringLeaseRequest,
    CodingAuthoringLeaseResponse,
    CodingBudgets,
    CodingCapabilityCertificationReceipt,
    CodingGraderExecutionReceipt,
    CodingGraderPlan,
    CodingGraderResourceProfile,
    CodingGradingLeaseRequest,
    CodingGradingLeaseResponse,
    CodingIssue,
    CodingRunEvidence,
    CodingRunManifest,
    CodingRunnerPlan,
    CodingRunRequest,
    CodingRuntimePolicy,
    CodingSeedRequest,
    CodingTaskEvidence,
    SubmitCodingAuthoringFreezeRequest,
    SubmitCodingAuthoringFreezeResponse,
    SubmitCodingCertificationRequest,
    SubmitCodingShadowResultRequest,
    SubmitCodingShadowResultResponse,
    canonical_digest,
    canonical_json_bytes,
    coding_authoring_evidence_digest,
    coding_authoring_freeze_signing_message,
    coding_authoring_lease_signing_message,
    coding_budgets_digest,
    coding_certification_receipt_digest,
    coding_certification_signing_message,
    coding_grading_lease_signing_message,
    coding_issue_digest,
    coding_run_evidence_transport_digest,
    coding_runtime_policy_digest,
    coding_shadow_result_signing_message,
    grader_execution_receipt_root,
    grader_plan_digest,
    grader_resource_profile_digest,
    memory_bundle_digest,
    parse_canonical_json,
    run_evidence_digest,
    runner_plan_digest,
    task_evidence_digest,
    validate_execution_plan_bundle,
    validate_run_evidence_against_manifest,
)

_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_contract_v1.json"
)
_CERTIFICATION_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_certification_v1.json"
)
_SELECTION_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_selection_v1.json"
)
_ARTIFACT_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_artifact_capability_v1.json"
)
_AUTHORING_FREEZE_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_authoring_freeze_v1.json"
)
_GRADING_LEASE_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_grading_lease_v1.json"
)
_SHADOW_RESULT_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_shadow_result_submission_v1.json"
)
_EXECUTION_PLAN_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_execution_plan_v1.json"
)


def _vectors() -> dict[str, Any]:
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def _body(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _authoring_response() -> dict[str, Any]:
    selection = json.loads(_SELECTION_VECTOR_PATH.read_text(encoding="utf-8"))
    artifacts = json.loads(_ARTIFACT_VECTOR_PATH.read_text(encoding="utf-8"))
    task = selection["task_version"]["payload"]
    return {
        "schema": "dittobench-coding-authoring-lease-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "ticket_id": artifacts["capabilities"][0]["ticket_id"],
        "ticket_deadline": artifacts["capabilities"][0]["ticket_deadline"],
        "coding_run_id": selection["run_manifest"]["coding_run_id"],
        "run_manifest_sha256": selection["run_authority"]["run_manifest_sha256"],
        "task_set_manifest_sha256": selection["run_manifest"][
            "task_set_manifest_sha256"
        ],
        "repository_epoch": task["repository_epoch"],
        "issue_sha256": task["issue_sha256"],
        "runtime_policy_sha256": task["runtime_policy_sha256"],
        "budgets_sha256": task["budgets_sha256"],
        "issue": selection["issue"],
        "runtime_policy": selection["runtime_policy"],
        "budgets": selection["budgets"],
        "run_manifest": selection["run_manifest"],
        "capabilities": artifacts["capabilities"][:3],
    }


def test_coding_certification_vector_matches_canonical_receipt_and_signature() -> None:
    vector = json.loads(_CERTIFICATION_VECTOR_PATH.read_text(encoding="utf-8"))
    receipt = parse_canonical_json(
        CodingCapabilityCertificationReceipt, _body(vector["receipt"])
    )
    expected = vector["expected"]
    assert (
        coding_certification_receipt_digest(receipt) == expected["certification_sha256"]
    )
    message = coding_certification_signing_message(
        validator_hotkey=expected["validator_hotkey"],
        agent_id=UUID(expected["agent_id"]),
        bench_version=expected["bench_version"],
        ticket_deadline=datetime.fromisoformat(expected["ticket_deadline"]),
        screened_image_sha256=expected["screened_image_sha256"],
        certification_sha256=receipt.certification_sha256,
    )
    assert hashlib.sha256(message).hexdigest() == expected["signing_message_sha256"]


def test_coding_certification_envelope_requires_aware_ticket_deadline() -> None:
    vector = json.loads(_CERTIFICATION_VECTOR_PATH.read_text(encoding="utf-8"))
    expected = vector["expected"]
    payload = {
        "validator_hotkey": expected["validator_hotkey"],
        "bench_version": expected["bench_version"],
        "ticket_deadline": expected["ticket_deadline"],
        "screened_image_sha256": expected["screened_image_sha256"],
        "receipt": vector["receipt"],
        "signature": "00" * 64,
    }
    SubmitCodingCertificationRequest.model_validate_json(json.dumps(payload))
    payload["ticket_deadline"] = "2026-08-20T16:30:00"
    with pytest.raises(ValidationError, match="timezone-aware"):
        SubmitCodingCertificationRequest.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("key", "model"),
    [
        ("manifest", CodingRunManifest),
        ("seed_request", CodingSeedRequest),
        ("run_request", CodingRunRequest),
    ],
)
def test_coding_v1_golden_vectors_have_stable_known_field_digests(
    key: str, model: type[CodingRunManifest]
) -> None:
    vectors = _vectors()
    parsed = parse_canonical_json(model, _body(vectors[key]))
    assert canonical_digest(parsed) == vectors["digests"][key]
    assert canonical_json_bytes(parsed).endswith(b"\n")


def test_private_selection_vector_matches_validator_run_manifest() -> None:
    vector = json.loads(_SELECTION_VECTOR_PATH.read_text(encoding="utf-8"))
    manifest = parse_canonical_json(CodingRunManifest, _body(vector["run_manifest"]))
    issue = CodingIssue.model_validate(vector["issue"])
    runtime_policy = CodingRuntimePolicy.model_validate(vector["runtime_policy"])
    budgets = CodingBudgets.model_validate(vector["budgets"])
    assert canonical_digest(manifest) == vector["run_authority"]["run_manifest_sha256"]
    assert (
        coding_issue_digest(issue) == vector["task_version"]["payload"]["issue_sha256"]
    )
    assert (
        coding_runtime_policy_digest(runtime_policy)
        == vector["task_version"]["payload"]["runtime_policy_sha256"]
    )
    assert (
        coding_budgets_digest(budgets)
        == vector["task_version"]["payload"]["budgets_sha256"]
    )


def test_authoring_request_and_response_match_platform_contract() -> None:
    requested_at = datetime.fromisoformat("2026-08-21T12:00:00+00:00")
    request = CodingAuthoringLeaseRequest(
        validator_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        ticket_id=UUID("33333333-3333-4333-8333-333333333333"),
        nonce=UUID("77777777-7777-4777-8777-777777777777"),
        requested_at=requested_at,
        signature="88" * 64,
    )
    assert coding_authoring_lease_signing_message(
        validator_hotkey=request.validator_hotkey,
        ticket_id=request.ticket_id,
        nonce=request.nonce,
        requested_at=request.requested_at,
    ) == (
        b"dittobench-coding-authoring-lease:v1\x00"
        b"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY\x00"
        b"33333333-3333-4333-8333-333333333333\x00"
        b"77777777-7777-4777-8777-777777777777\x00"
        b"2026-08-21T12:00:00.000000+00:00"
    )

    response = CodingAuthoringLeaseResponse.model_validate_json(
        json.dumps(_authoring_response())
    )
    assert [item.artifact_kind.value for item in response.capabilities] == [
        "visible-bundle",
        "memory-bundle",
        "resource-profile",
    ]
    assert "grader-bundle" not in response.model_dump_json()


def test_authoring_freeze_vector_matches_platform_contract() -> None:
    vector = json.loads(_AUTHORING_FREEZE_VECTOR_PATH.read_text(encoding="utf-8"))
    request = SubmitCodingAuthoringFreezeRequest.model_validate_json(
        json.dumps(vector["request"])
    )
    response = SubmitCodingAuthoringFreezeResponse.model_validate_json(
        json.dumps(vector["response"])
    )
    evidence = CodingAuthoringEvidence.model_validate_json(
        json.dumps(vector["request"]["evidence"])
    )
    assert (
        coding_authoring_evidence_digest(evidence)
        == (vector["expected"]["authoring_evidence_sha256"])
    )
    message = coding_authoring_freeze_signing_message(
        validator_hotkey=request.validator_hotkey,
        agent_id=UUID(vector["agent_id"]),
        bench_version=request.bench_version,
        run_row_id=request.run_row_id,
        ticket_id=request.ticket_id,
        ticket_deadline=request.ticket_deadline,
        coding_run_id=request.coding_run_id,
        agent_artifact_sha256=request.agent_artifact_sha256,
        screened_image_sha256=request.screened_image_sha256,
        run_manifest_sha256=request.run_manifest_sha256,
        task_set_manifest_sha256=request.task_set_manifest_sha256,
        authoring_evidence_sha256=request.authoring_evidence_sha256,
        authoring_transcript_object_key=request.authoring_transcript_object_key,
        authoring_transcript_bytes=request.authoring_transcript_bytes,
        authoring_event_count=request.authoring_event_count,
        frozen_submission_object_key=request.frozen_submission_object_key,
    )
    assert (
        hashlib.sha256(message).hexdigest()
        == (vector["expected"]["signing_message_sha256"])
    )
    assert response.ticket_id == request.ticket_id


def test_authoring_evidence_rejects_solver_route_substitution() -> None:
    raw = json.loads(_AUTHORING_FREEZE_VECTOR_PATH.read_text(encoding="utf-8"))[
        "request"
    ]["evidence"]
    for field, value in (
        ("model", "openai/another-model"),
        ("provider", "other-route"),
        ("provider_route_profile", "other-profile-v1"),
    ):
        changed = copy.deepcopy(raw)
        changed["model"][field] = value
        with pytest.raises(ValidationError):
            CodingAuthoringEvidence.model_validate(changed)


def test_grading_lease_vector_matches_platform_contract() -> None:
    vector = json.loads(_GRADING_LEASE_VECTOR_PATH.read_text(encoding="utf-8"))
    request = CodingGradingLeaseRequest.model_validate_json(
        json.dumps(vector["request"])
    )
    response = CodingGradingLeaseResponse.model_validate_json(
        json.dumps(vector["response"])
    )
    message = coding_grading_lease_signing_message(
        validator_hotkey=request.validator_hotkey,
        agent_id=request.agent_id,
        run_row_id=request.run_row_id,
        ticket_id=request.ticket_id,
        freeze_id=request.freeze_id,
        authoring_evidence_sha256=request.authoring_evidence_sha256,
        nonce=request.nonce,
        requested_at=request.requested_at,
    )
    assert (
        hashlib.sha256(message).hexdigest()
        == vector["expected"]["signing_message_sha256"]
    )
    assert [capability.artifact_kind.value for capability in response.capabilities] == [
        "visible-bundle",
        "resource-profile",
        "grader-bundle",
    ]
    assert "memory-bundle" not in response.model_dump_json()


def test_shadow_result_submission_vector_matches_platform_contract() -> None:
    vector = json.loads(_SHADOW_RESULT_VECTOR_PATH.read_text(encoding="utf-8"))
    request = SubmitCodingShadowResultRequest.model_validate_json(
        json.dumps(vector["request"])
    )
    response = SubmitCodingShadowResultResponse.model_validate_json(
        json.dumps(vector["response"])
    )
    manifest = CodingRunManifest.model_validate_json(
        json.dumps(vector["authority"]["run_manifest"])
    )
    task_evidence = [
        CodingTaskEvidence.model_validate_json(json.dumps(item))
        for item in vector["authority"]["task_evidence"]
    ]
    assert (
        run_evidence_digest(
            manifest,
            str(request.ticket_id),
            request.evidence,
            task_evidence,
        )
        == coding_run_evidence_transport_digest(request.evidence)
        == vector["expected"]["run_evidence_sha256"]
        == request.run_evidence_sha256
    )
    message = coding_shadow_result_signing_message(
        validator_hotkey=request.validator_hotkey,
        agent_id=UUID(vector["agent_id"]),
        run_row_id=request.run_row_id,
        ticket_id=request.ticket_id,
        bench_version=request.bench_version,
        ticket_deadline=request.ticket_deadline,
        agent_artifact_sha256=request.agent_artifact_sha256,
        screened_image_sha256=request.screened_image_sha256,
        run_evidence_sha256=request.run_evidence_sha256,
    )
    assert (
        hashlib.sha256(message).hexdigest()
        == vector["expected"]["signing_message_sha256"]
    )
    assert response.ticket_id == request.ticket_id
    extended = {**vector["request"], "future_result_hint": "ignored"}
    assert (
        "future_result_hint"
        not in SubmitCodingShadowResultRequest.model_validate_json(
            json.dumps(extended)
        ).model_fields_set
    )


def test_evidence_golden_vectors_require_manifest_authority() -> None:
    vectors = _vectors()
    manifest = parse_canonical_json(CodingRunManifest, _body(vectors["manifest"]))
    task = parse_canonical_json(CodingTaskEvidence, _body(vectors["task_evidence"]))
    run = parse_canonical_json(CodingRunEvidence, _body(vectors["run_evidence"]))
    ticket = "validator-ticket-001"
    assert (
        task_evidence_digest(manifest, ticket, task)
        == vectors["digests"]["task_evidence"]
    )
    assert (
        run_evidence_digest(manifest, ticket, run, [task])
        == vectors["digests"]["run_evidence"]
    )
    with pytest.raises(TypeError, match="manifest-bound"):
        canonical_digest(task)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="transport models"):
        canonical_digest(task.model_dump(mode="json"))  # type: ignore[arg-type]

    incomplete = copy.deepcopy(vectors["task_evidence"])
    incomplete["grader"]["execution_receipt_count"] = 5
    with pytest.raises(ValidationError, match="passing repair"):
        parse_canonical_json(CodingTaskEvidence, _body(incomplete))


def test_grader_plan_resource_and_receipt_vectors_are_independently_replayable() -> (
    None
):
    vectors = _vectors()
    plan = parse_canonical_json(CodingGraderPlan, _body(vectors["grader_plan"]))
    resource = parse_canonical_json(
        CodingGraderResourceProfile, _body(vectors["grader_resource_profile"])
    )
    receipts = [
        parse_canonical_json(CodingGraderExecutionReceipt, _body(receipt))
        for receipt in vectors["grader_execution_receipts"]
    ]
    assert grader_plan_digest(plan) == vectors["digests"]["grader_plan"]
    assert (
        grader_resource_profile_digest(resource)
        == vectors["digests"]["grader_resource_profile"]
        == plan.resource_profile_sha256
    )
    assert (
        grader_execution_receipt_root(plan, receipts)
        == vectors["digests"]["grader_execution_receipt_root"]
    )

    broken = receipts.copy()
    broken[1] = broken[1].model_copy(update={"previous_receipt_sha256": "f" * 64})
    with pytest.raises(ValueError, match="receipt chain"):
        grader_execution_receipt_root(plan, broken)


def test_execution_plan_vector_binds_phase_separated_authority() -> None:
    vector = json.loads(_EXECUTION_PLAN_VECTOR_PATH.read_text(encoding="utf-8"))
    assert vector["schema"] == "dittobench-coding-execution-plan-vector-v1"
    assert vector["coding_contract_version"] == 1
    assert vector["weight_eligible"] is False
    runner = parse_canonical_json(CodingRunnerPlan, _body(vector["runner_plan"]))
    runtime = parse_canonical_json(CodingRuntimePolicy, _body(vector["runtime_policy"]))
    grader = parse_canonical_json(CodingGraderPlan, _body(vector["grader_plan"]))
    resource = parse_canonical_json(
        CodingGraderResourceProfile, _body(vector["grader_resource_profile"])
    )
    assert runner_plan_digest(runner) == vector["expected"]["runner_plan_sha256"]
    assert grader_plan_digest(grader) == vector["expected"]["grader_plan_sha256"]
    assert (
        grader_resource_profile_digest(resource)
        == vector["expected"]["grader_resource_profile_sha256"]
    )
    validate_execution_plan_bundle(
        runner_plan=runner,
        runtime_policy=runtime,
        grader_plan=grader,
        resource_profile=resource,
    )
    assert "grader/adversarial.py" not in repr(grader)

    extended = copy.deepcopy(vector["runner_plan"])
    extended["future_diagnostic"] = {"ignored": True}
    extended["test_commands"][0]["future_hint"] = "ignored"
    parsed = parse_canonical_json(CodingRunnerPlan, _body(extended))
    assert runner_plan_digest(parsed) == runner_plan_digest(runner)


def test_execution_plan_rejects_authority_drift_and_unsafe_commands() -> None:
    vector = json.loads(_EXECUTION_PLAN_VECTOR_PATH.read_text(encoding="utf-8"))
    runner = parse_canonical_json(CodingRunnerPlan, _body(vector["runner_plan"]))
    runtime = parse_canonical_json(CodingRuntimePolicy, _body(vector["runtime_policy"]))
    grader = parse_canonical_json(CodingGraderPlan, _body(vector["grader_plan"]))
    resource = parse_canonical_json(
        CodingGraderResourceProfile, _body(vector["grader_resource_profile"])
    )

    for changed in {
        "runtime paths": runtime.model_copy(update={"editable_paths": []}),
        "runtime tests": runtime.model_copy(update={"test_command_ids": []}),
        "runner limits": runner.model_copy(
            update={
                "limits": runner.limits.model_copy(
                    update={"max_workspace_bytes": 2 << 30}
                )
            }
        ),
        "grader case": grader.model_copy(update={"case_id": "other-case"}),
        "grader resource": grader.model_copy(
            update={"resource_profile_sha256": "f" * 64}
        ),
    }.values():
        kwargs = {
            "runner_plan": runner,
            "runtime_policy": runtime,
            "grader_plan": grader,
            "resource_profile": resource,
        }
        if isinstance(changed, CodingRunnerPlan):
            kwargs["runner_plan"] = changed
        elif isinstance(changed, CodingRuntimePolicy):
            kwargs["runtime_policy"] = changed
        else:
            kwargs["grader_plan"] = changed
        with pytest.raises((ValidationError, ValueError), match="plan|workspace"):
            validate_execution_plan_bundle(**kwargs)  # type: ignore[arg-type]

    unsafe = copy.deepcopy(vector["runner_plan"])
    unsafe["test_commands"][0]["argv"] = ["sh", "-c", "pytest"]
    with pytest.raises(ValidationError, match="executable"):
        parse_canonical_json(CodingRunnerPlan, _body(unsafe))

    overlapping = copy.deepcopy(vector["runner_plan"])
    overlapping["creatable_paths"] = ["src/parser.py"]
    with pytest.raises(ValidationError, match="path sets"):
        parse_canonical_json(CodingRunnerPlan, _body(overlapping))


def test_execution_plan_vector_is_validator_only_and_capability_free() -> None:
    body = _EXECUTION_PLAN_VECTOR_PATH.read_text(encoding="utf-8")
    for forbidden in (
        '"ticket_id"',
        '"memories"',
        '"memory_bundle',
        '"url"',
        '"bearer"',
        '"signature"',
        "OPENROUTER_API_KEY",
    ):
        assert forbidden not in body
    rust_root = Path(__file__).parents[3] / "miners/dittobench-coding-starter-kit"
    for source in rust_root.rglob("*.rs"):
        assert "coding_execution_plan_v1.json" not in source.read_text(encoding="utf-8")


def test_unknown_fields_are_ignored_and_excluded_from_canonical_digest() -> None:
    vectors = _vectors()
    original = parse_canonical_json(CodingRunManifest, _body(vectors["manifest"]))
    extended = copy.deepcopy(vectors["manifest"])
    extended["future_unsigned_diagnostic"] = {"value": 1}
    extended["tasks"][0]["future_transport_hint"] = "ignored"
    parsed = parse_canonical_json(CodingRunManifest, _body(extended))
    assert canonical_digest(parsed) == canonical_digest(original)


def test_unicode_and_html_characters_have_cross_language_canonical_bytes() -> None:
    vectors = _vectors()
    request = parse_canonical_json(CodingRunRequest, _body(vectors["run_request"]))
    issue = request.issue.model_copy(
        update={"description": "Preserve café <tag> & separators \u2028 and \u2029."}
    )
    mutated = request.model_copy(update={"issue": issue})
    assert canonical_digest(mutated) == vectors["digests"]["unicode_run_request"]

    memories = copy.deepcopy(vectors["seed_request"]["memories"])
    memories[0]["content"] = "Preserve café <tag> & separators \u2028 and \u2029."
    assert memory_bundle_digest(memories) == vectors["digests"]["unicode_seed_memory"]


def test_raw_unicode_boundary_vectors_match_go() -> None:
    vectors = _vectors()
    boundaries = vectors["wire_boundary_vectors"]
    original = b'"The parser drops an incomplete trailing sequence."'

    def replace_description(raw_json_string: str) -> bytes:
        return _body(vectors["run_request"]).replace(
            original, raw_json_string.encode("utf-8"), 1
        )

    paired = parse_canonical_json(
        CodingRunRequest,
        replace_description(boundaries["paired_surrogate_json_string"]),
    )
    assert paired.issue.description == "😀"
    for key, expected in (
        ("escaped_surrogate_literal_json_string", r"\ud800"),
        ("replacement_character_json_string", "�"),
    ):
        parsed = parse_canonical_json(
            CodingRunRequest, replace_description(boundaries[key])
        )
        assert parsed.issue.description == expected
    for key in ("lone_high_json_string", "lone_low_json_string"):
        with pytest.raises((ValueError, UnicodeError)):
            parse_canonical_json(CodingRunRequest, replace_description(boundaries[key]))

    invalid_utf8 = replace_description('"invalid"').replace(b"invalid", b"\xff", 1)
    with pytest.raises((ValueError, UnicodeError)):
        parse_canonical_json(CodingRunRequest, invalid_utf8)


def test_duplicate_and_missing_known_fields_fail_closed() -> None:
    duplicate = b'{"schema":"a","schema":"b"}'
    with pytest.raises(ValueError, match="duplicate JSON field"):
        parse_canonical_json(CodingRunManifest, duplicate)

    manifest = _vectors()["manifest"]
    del manifest["weight_eligible"]
    with pytest.raises(ValidationError):
        parse_canonical_json(CodingRunManifest, _body(manifest))


def test_shadow_contract_cannot_become_weight_eligible() -> None:
    manifest = _vectors()["manifest"]
    manifest["weight_eligible"] = True
    with pytest.raises(ValidationError):
        parse_canonical_json(CodingRunManifest, _body(manifest))

    manifest = _vectors()["manifest"]
    manifest["bench_family"] = "memory"
    with pytest.raises(ValidationError):
        parse_canonical_json(CodingRunManifest, _body(manifest))


def test_canonical_digest_revalidates_mutable_nested_collections() -> None:
    vectors = _vectors()
    manifest = parse_canonical_json(CodingRunManifest, _body(vectors["manifest"]))
    manifest.tasks.append(manifest.tasks[0])
    with pytest.raises(ValidationError, match="unique and sorted"):
        canonical_digest(manifest)


def test_resolved_task_requires_complete_passing_grader_evidence() -> None:
    evidence = _vectors()["task_evidence"]
    evidence["grader"]["test_groups"][1]["passed"] = 2
    with pytest.raises(ValidationError, match="complete passing repair"):
        parse_canonical_json(CodingTaskEvidence, _body(evidence))


def test_infrastructure_and_invalid_tasks_are_not_in_repair_mean() -> None:
    evidence = _vectors()["run_evidence"]
    evidence["tasks"].append(
        {
            "case_id": "case-002",
            "variant_id": "variant-v1",
            "task_evidence_sha256": "9" * 64,
            "terminal_domain": "validator_infrastructure",
            "repair_score_micros": 0,
        }
    )
    evidence["tasks"].append(
        {
            "case_id": "case-003",
            "variant_id": "variant-v1",
            "task_evidence_sha256": "8" * 64,
            "terminal_domain": "task_invalid",
            "repair_score_micros": 0,
        }
    )
    evidence["tasks"].append(
        {
            "case_id": "case-004",
            "variant_id": "variant-v1",
            "task_evidence_sha256": "7" * 64,
            "terminal_domain": "candidate_integrity",
            "repair_score_micros": 0,
        }
    )
    evidence["tasks"].append(
        {
            "case_id": "case-005",
            "variant_id": "variant-v1",
            "task_evidence_sha256": "6" * 64,
            "terminal_domain": "control_plane_integrity",
            "repair_score_micros": 0,
        }
    )
    evidence["infrastructure_count"] = 1
    evidence["invalid_count"] = 1
    evidence["candidate_integrity_count"] = 1
    evidence["control_plane_integrity_count"] = 1
    evidence["scoreable_task_count"] = 2
    evidence["repair_mean_micros"] = 500_000
    parsed = parse_canonical_json(CodingRunEvidence, _body(evidence))
    assert parsed.scoreable_task_count == 2
    assert parsed.repair_mean_micros == 500_000


def test_shared_nonresolved_and_aggregate_evidence_vectors() -> None:
    vectors = _vectors()
    manifest = parse_canonical_json(CodingRunManifest, _body(vectors["manifest"]))
    nonresolved = parse_canonical_json(
        CodingTaskEvidence, _body(vectors["nonresolved_task_evidence"])
    )
    assert nonresolved.authoring is None
    assert nonresolved.grader is None
    assert nonresolved.terminal_domain.value == "validator_infrastructure"
    assert nonresolved.failure_code == "transport_pre_authoritative"
    assert (
        task_evidence_digest(manifest, "validator-ticket-001", nonresolved)
        == vectors["digests"]["nonresolved_task_evidence"]
    )

    aggregate = parse_canonical_json(
        CodingRunEvidence, _body(vectors["aggregate_run_evidence"])
    )
    assert aggregate.scoreable_task_count == 6
    assert aggregate.repair_mean_micros == 666_666
    assert (
        aggregate.resolved_count,
        aggregate.repair_failure_count,
        aggregate.infrastructure_count,
        aggregate.invalid_count,
        aggregate.candidate_integrity_count,
        aggregate.control_plane_integrity_count,
    ) == (4, 1, 1, 1, 1, 1)
    aggregate_bytes = (
        json.dumps(
            aggregate.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        + "\n"
    ).encode()
    assert (
        hashlib.sha256(aggregate_bytes).hexdigest()
        == vectors["digests"]["aggregate_run_evidence"]
    )


def test_run_evidence_replays_against_manifest_and_task_roots() -> None:
    vectors = _vectors()
    manifest = parse_canonical_json(CodingRunManifest, _body(vectors["manifest"]))
    task = parse_canonical_json(CodingTaskEvidence, _body(vectors["task_evidence"]))
    run = parse_canonical_json(CodingRunEvidence, _body(vectors["run_evidence"]))
    validate_run_evidence_against_manifest(
        manifest, "validator-ticket-001", run, [task]
    )

    mismatched = run.model_copy(
        update={"task_set_manifest_sha256": "0" * 64}, deep=True
    )
    with pytest.raises(ValueError, match="task-set digest"):
        validate_run_evidence_against_manifest(
            manifest, "validator-ticket-001", mismatched, [task]
        )

    with pytest.raises(ValueError, match="lease authority"):
        validate_run_evidence_against_manifest(manifest, "other-ticket", run, [task])

    changed_grant = manifest.model_copy(
        update={"inference_grant_sha256": "1" * 64}, deep=True
    )
    with pytest.raises(ValueError, match="inference grant"):
        task_evidence_digest(changed_grant, "validator-ticket-001", task)

    assert task.grader is not None
    for field, value in {
        "grader_bundle_sha256": "1" * 64,
        "grader_image_digest": "sha256:" + "1" * 64,
        "grader_platform": "linux/arm64",
        "test_manifest_sha256": "1" * 64,
        "grader_plan_sha256": "1" * 64,
        "resource_profile_sha256": "1" * 64,
    }.items():
        changed_grader = task.grader.model_copy(update={field: value})
        changed_task = task.model_copy(update={"grader": changed_grader})
        with pytest.raises(
            (ValueError, ValidationError), match="grader|manifest|linux/amd64"
        ):
            task_evidence_digest(manifest, "validator-ticket-001", changed_task)


def test_zero_model_attempt_has_canonical_attributable_evidence() -> None:
    vectors = _vectors()
    manifest = parse_canonical_json(CodingRunManifest, _body(vectors["manifest"]))
    evidence = parse_canonical_json(
        CodingTaskEvidence, _body(vectors["zero_model_task_evidence"])
    )
    assert evidence.authoring is not None
    assert evidence.authoring.model.requests == 0
    assert (
        task_evidence_digest(manifest, "validator-ticket-001", evidence)
        == vectors["digests"]["zero_model_task_evidence"]
    )
    invalid = copy.deepcopy(vectors["zero_model_task_evidence"])
    invalid["authoring"]["model"]["requests"] = 1
    with pytest.raises(ValidationError, match="canonical zero accounting"):
        parse_canonical_json(CodingTaskEvidence, _body(invalid))


def test_python_integer_bounds_match_go_wire_widths() -> None:
    vectors = _vectors()
    manifest = vectors["manifest"]
    manifest["selection_block_number"] = 1 << 64
    with pytest.raises(ValidationError):
        parse_canonical_json(CodingRunManifest, _body(manifest))

    task = vectors["task_evidence"]
    task["grader"]["test_groups"][0]["total"] = 1 << 32
    with pytest.raises(ValidationError):
        parse_canonical_json(CodingTaskEvidence, _body(task))


def test_null_collections_and_excessive_nesting_fail_closed() -> None:
    vectors = _vectors()
    run = vectors["run_request"]
    run["issue"]["constraints"] = None
    with pytest.raises(ValidationError):
        parse_canonical_json(CodingRunRequest, _body(run))

    def manifest_at_value_depth(value_depth: int) -> dict[str, Any]:
        nested: object = "leaf"
        for _ in range(value_depth - 1):
            nested = [nested]
        manifest = copy.deepcopy(vectors["manifest"])
        manifest["future_nested"] = nested
        return manifest

    boundaries = vectors["wire_boundary_vectors"]
    parse_canonical_json(
        CodingRunManifest,
        _body(manifest_at_value_depth(boundaries["max_json_depth"])),
    )
    with pytest.raises(ValueError, match="nesting"):
        parse_canonical_json(
            CodingRunManifest,
            _body(manifest_at_value_depth(boundaries["reject_json_depth"])),
        )
