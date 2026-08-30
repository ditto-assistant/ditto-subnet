"""Tests for validator-owned shadow coding terminal aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ditto.api_models.coding import (
    CodingRunEvidence,
    CodingRunManifest,
    CodingTaskEvidence,
    CodingTerminalDomain,
    coding_run_evidence_transport_digest,
)
from ditto.validator.coding_terminal import (
    CodingTerminalEvidenceError,
    build_coding_run_evidence,
)

_TESTDATA = (
    Path(__file__).parents[3] / "packages" / "dittobench-coding-contract" / "testdata"
)


def _vector(name: str) -> dict:
    return json.loads((_TESTDATA / name).read_text(encoding="utf-8"))


def test_builder_reproduces_result_submission_vector() -> None:
    vector = _vector("coding_shadow_result_submission_v1.json")
    manifest = CodingRunManifest.model_validate_json(
        json.dumps(vector["authority"]["run_manifest"])
    )
    tasks = [
        CodingTaskEvidence.model_validate_json(json.dumps(item))
        for item in vector["authority"]["task_evidence"]
    ]
    evidence = build_coding_run_evidence(
        manifest,
        vector["request"]["ticket_id"],
        tasks,
    )
    assert evidence == CodingRunEvidence.model_validate_json(
        json.dumps(vector["request"]["evidence"])
    )
    assert (
        coding_run_evidence_transport_digest(evidence)
        == vector["expected"]["run_evidence_sha256"]
    )


def test_builder_reproduces_resolved_contract_vector() -> None:
    vector = _vector("coding_contract_v1.json")
    manifest = CodingRunManifest.model_validate_json(json.dumps(vector["manifest"]))
    task = CodingTaskEvidence.model_validate_json(json.dumps(vector["task_evidence"]))
    assert build_coding_run_evidence(
        manifest,
        vector["run_evidence"]["validator_ticket_id"],
        [task],
    ) == CodingRunEvidence.model_validate_json(json.dumps(vector["run_evidence"]))


def test_builder_derives_mixed_domain_counts_and_binary_mean() -> None:
    vector = _vector("coding_contract_v1.json")
    base_manifest = CodingRunManifest.model_validate_json(
        json.dumps(vector["manifest"])
    )
    base_task = CodingTaskEvidence.model_validate_json(
        json.dumps(vector["task_evidence"])
    )
    ticket_id = "mixed-validator-ticket"
    coding_run_id = "mixed-coding-run"
    domains = (
        CodingTerminalDomain.RESOLVED,
        CodingTerminalDomain.REPAIR_FAILURE,
        CodingTerminalDomain.CANDIDATE_INTEGRITY,
        CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE,
    )
    selected = []
    tasks = []
    for index, domain in enumerate(domains, start=1):
        manifest_task = base_manifest.tasks[0].model_copy(
            update={"case_id": f"case-{index:03d}"}
        )
        selected.append(manifest_task)
        tasks.append(
            base_task.model_copy(
                update={
                    "coding_run_id": coding_run_id,
                    "validator_ticket_id": ticket_id,
                    "task": manifest_task,
                    "terminal_domain": domain,
                    "failure_code": (
                        None if domain is CodingTerminalDomain.RESOLVED else "failed"
                    ),
                    "repair_score_micros": (
                        1_000_000 if domain is CodingTerminalDomain.RESOLVED else 0
                    ),
                }
            )
        )
    manifest = base_manifest.model_copy(
        update={"coding_run_id": coding_run_id, "tasks": selected}
    )

    evidence = build_coding_run_evidence(manifest, ticket_id, tasks)
    assert evidence.resolved_count == 1
    assert evidence.repair_failure_count == 1
    assert evidence.candidate_integrity_count == 1
    assert evidence.infrastructure_count == 1
    assert evidence.scoreable_task_count == 3
    assert evidence.repair_mean_micros == 333_333


@pytest.mark.parametrize("drift", ["missing", "duplicate", "artifact"])
def test_builder_rejects_incomplete_duplicate_or_drifted_evidence(
    drift: str,
) -> None:
    vector = _vector("coding_shadow_result_submission_v1.json")
    manifest = CodingRunManifest.model_validate_json(
        json.dumps(vector["authority"]["run_manifest"])
    )
    task = CodingTaskEvidence.model_validate_json(
        json.dumps(vector["authority"]["task_evidence"][0])
    )
    tasks: list[CodingTaskEvidence]
    if drift == "missing":
        tasks = []
    elif drift == "duplicate":
        tasks = [task, task]
    else:
        tasks = [task.model_copy(update={"agent_artifact_sha256": "ff" * 32})]
    with pytest.raises(CodingTerminalEvidenceError, match="immutable"):
        build_coding_run_evidence(
            manifest,
            vector["request"]["ticket_id"],
            tasks,
        )
