from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ditto.api_models.coding import (
    CodingRunEvidence,
    CodingRunManifest,
    CodingRunRequest,
    CodingSeedRequest,
    CodingTaskEvidence,
    canonical_digest,
    canonical_json_bytes,
    parse_canonical_json,
    run_evidence_digest,
    task_evidence_digest,
    validate_run_evidence_against_manifest,
)

_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_contract_v1.json"
)


def _vectors() -> dict[str, Any]:
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def _body(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


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
    assert (
        canonical_digest({"memories": memories})
        == vectors["digests"]["unicode_seed_memory"]
    )


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

    nested: object = "leaf"
    for _ in range(130):
        nested = [nested]
    manifest = vectors["manifest"]
    manifest["future_nested"] = nested
    with pytest.raises(ValueError, match="nesting"):
        parse_canonical_json(CodingRunManifest, _body(manifest))
