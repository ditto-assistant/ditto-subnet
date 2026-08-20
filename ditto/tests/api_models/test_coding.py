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
    CodingCapabilityCertificationReceipt,
    CodingGraderExecutionReceipt,
    CodingGraderPlan,
    CodingGraderResourceProfile,
    CodingRunEvidence,
    CodingRunManifest,
    CodingRunRequest,
    CodingSeedRequest,
    CodingTaskEvidence,
    SubmitCodingCertificationRequest,
    canonical_digest,
    canonical_json_bytes,
    coding_certification_receipt_digest,
    coding_certification_signing_message,
    grader_execution_receipt_root,
    grader_plan_digest,
    grader_resource_profile_digest,
    memory_bundle_digest,
    parse_canonical_json,
    run_evidence_digest,
    task_evidence_digest,
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


def _vectors() -> dict[str, Any]:
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def _body(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


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
