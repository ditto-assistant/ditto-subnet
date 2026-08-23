#!/usr/bin/env python3
"""Regenerate the execution-plan-linked private selection vector."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "apps/platform"
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from ditto.api_models.coding_catalog import (  # noqa: E402
    CodingCatalogCommitment,
    coding_catalog_commitment_digest,
)
from ditto.api_models.coding_selection import (  # noqa: E402
    CodingCatalogGraderPlan,
    CodingCatalogManifestTask,
    CodingCatalogMembershipProof,
    CodingCatalogResourceProfile,
    CodingCatalogRunnerPlan,
    CodingCatalogRuntimePolicy,
    CodingCatalogTaskPayload,
    CodingCatalogTaskVersion,
    CodingSelectionRunManifest,
    bind_coding_selection_assignment,
    coding_catalog_grader_plan_digest,
    coding_catalog_membership_proof_digest,
    coding_catalog_resource_profile_digest,
    coding_catalog_runner_plan_digest,
    coding_catalog_task_commitment_digest,
    coding_selection_run_manifest_digest,
    validate_coding_catalog_execution_bundle,
)
from ditto.coding_selection import (  # noqa: E402
    coding_catalog_leaf_hash,
    coding_catalog_node_hash,
    coding_selection_catalog_index,
    coding_selection_seed_sha256,
    rebuild_coding_selection_result,
)

TESTDATA = Path(__file__).resolve().parent / "testdata"
EXECUTION_PATH = TESTDATA / "coding_execution_plan_v1.json"
SELECTION_PATH = TESTDATA / "coding_selection_v1.json"
GRADING_PATH = TESTDATA / "coding_grading_lease_v1.json"
ARTIFACT_PATH = TESTDATA / "coding_artifact_capability_v1.json"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _root_from_proof(
    *, catalog_index: int, task_commitment: str, siblings: list[str]
) -> str:
    node = coding_catalog_leaf_hash(
        catalog_index=catalog_index,
        task_commitment_sha256=task_commitment,
    )
    for level, sibling in enumerate(siblings):
        if (catalog_index >> level) & 1:
            node = coding_catalog_node_hash(
                level=level, left_sha256=sibling, right_sha256=node
            )
        else:
            node = coding_catalog_node_hash(
                level=level, left_sha256=node, right_sha256=sibling
            )
    return node


def vectors() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    execution = _load(EXECUTION_PATH)
    selection = _load(SELECTION_PATH)
    runner = CodingCatalogRunnerPlan.model_validate(execution["runner_plan"])
    grader = CodingCatalogGraderPlan.model_validate(execution["grader_plan"])
    resource = CodingCatalogResourceProfile.model_validate(
        execution["grader_resource_profile"]
    )
    runtime_policy = CodingCatalogRuntimePolicy.model_validate(
        selection["runtime_policy"]
    )
    validate_coding_catalog_execution_bundle(
        runner_plan=runner,
        runtime_policy=runtime_policy,
        grader_plan=grader,
        resource_profile=resource,
    )
    runner_sha = coding_catalog_runner_plan_digest(runner)
    grader_sha = coding_catalog_grader_plan_digest(grader)
    resource_sha = coding_catalog_resource_profile_digest(resource)
    execution["expected"] = {
        "runner_plan_sha256": runner_sha,
        "grader_plan_sha256": grader_sha,
        "grader_resource_profile_sha256": resource_sha,
    }

    previous_payload = selection["task_version"]["payload"]
    previous_task = previous_payload["task"]
    task = CodingCatalogManifestTask.model_validate(
        {
            **previous_task,
            "case_id": runner.case_id,
            "visible_bundle_sha256": runner.visible_bundle_sha256,
            "base_tree_sha256": runner.base_tree_sha256,
            "resource_profile_sha256": resource_sha,
            "grader_bundle_sha256": grader.grader_bundle_sha256,
            "grader_image_digest": grader.grader_image_digest,
            "test_manifest_sha256": grader.test_manifest_sha256,
            "grader_plan_sha256": grader_sha,
        }
    )
    payload = CodingCatalogTaskPayload.model_validate(
        {
            **previous_payload,
            "runner_plan_sha256": runner_sha,
            "task": task.model_dump(mode="json"),
        }
    )
    task_version = CodingCatalogTaskVersion(
        payload=payload,
        task_commitment_sha256=coding_catalog_task_commitment_digest(payload),
    )

    previous_proof = selection["membership_proof"]
    root = _root_from_proof(
        catalog_index=payload.catalog_index,
        task_commitment=task_version.task_commitment_sha256,
        siblings=previous_proof["sibling_sha256"],
    )
    proof_draft = CodingCatalogMembershipProof.model_construct(
        schema_name="dittobench-coding-catalog-membership-proof-v1",
        coding_contract_version=1,
        corpus_release_id=payload.corpus_release_id,
        catalog_merkle_root=root,
        task_version_count=previous_proof["task_version_count"],
        catalog_index=payload.catalog_index,
        task_commitment_sha256=task_version.task_commitment_sha256,
        sibling_sha256=previous_proof["sibling_sha256"],
        catalog_membership_proof_sha256="0" * 64,
    )
    proof = CodingCatalogMembershipProof.model_validate(
        {
            **proof_draft.model_dump(mode="json", by_alias=True),
            "catalog_membership_proof_sha256": coding_catalog_membership_proof_digest(
                proof_draft
            ),
        }
    )

    previous_commitment = selection["commitment"]
    commitment_draft = CodingCatalogCommitment.model_construct(
        **{
            **previous_commitment,
            "catalog_merkle_root": root,
            "grader_contract_sha256": grader.grader_contract_sha256,
            "commitment_sha256": "0" * 64,
        }
    )
    commitment = CodingCatalogCommitment.model_validate(
        {
            **commitment_draft.model_dump(mode="json", by_alias=True),
            "commitment_sha256": coding_catalog_commitment_digest(commitment_draft),
        }
    )

    assignment_fields = {
        **selection["assignment"],
        "catalog_commitment_sha256": commitment.commitment_sha256,
    }
    assignment_fields.pop("assignment_sha256", None)
    selection_block_hash = selection["selection_proof"]["selection_block_hash"]
    for counter in range(10_000):
        assignment_fields["anchor_block_hash"] = "0x" + f"{counter:064x}"
        assignment = bind_coding_selection_assignment(assignment_fields)
        seed = coding_selection_seed_sha256(
            assignment=assignment,
            commitment=commitment,
            selection_block_hash=selection_block_hash,
        )
        if (
            coding_selection_catalog_index(
                selection_seed_sha256=seed,
                task_version_count=commitment.task_version_count,
                probe=0,
            )
            == payload.catalog_index
        ):
            break
    else:  # pragma: no cover - permutation guarantees one quickly for this fixture
        raise RuntimeError("could not stabilize the synthetic first selection probe")
    probe = 0
    rebuilt = rebuild_coding_selection_result(
        assignment=assignment,
        commitment=commitment,
        selection_block_hash=selection_block_hash,
        candidate_probe=probe,
        task_version=task_version,
        membership=proof,
    )
    selection.update(
        {
            "commitment": commitment.model_dump(mode="json", by_alias=True),
            "assignment": assignment.model_dump(mode="json", by_alias=True),
            "task_version": task_version.model_dump(mode="json", by_alias=True),
            "membership_proof": proof.model_dump(mode="json", by_alias=True),
            "selection_seed_sha256": seed,
            "selection_proof": rebuilt.selection_proof.model_dump(
                mode="json", by_alias=True
            ),
            "task_set_manifest": rebuilt.task_set_manifest.model_dump(
                mode="json", by_alias=True
            ),
            "run_manifest": rebuilt.run_manifest.model_dump(mode="json", by_alias=True),
            "run_authority": rebuilt.authority.model_dump(mode="json", by_alias=True),
            "exposure": rebuilt.exposure.model_dump(mode="json", by_alias=True),
        }
    )
    artifacts = _load(ARTIFACT_PATH)
    for capability in artifacts["capabilities"]:
        if capability["artifact_kind"] != "resource-profile":
            continue
        previous = capability["sha256"]
        capability["sha256"] = resource_sha
        capability["url"] = capability["url"].replace(previous, resource_sha)
    grading = _load(GRADING_PATH)
    grading_manifest = CodingSelectionRunManifest.model_validate(
        selection["run_manifest"]
    )
    grading["response"].update(
        {
            "run_manifest_sha256": coding_selection_run_manifest_digest(
                grading_manifest
            ),
            "task_set_manifest_sha256": grading_manifest.task_set_manifest_sha256,
            "run_manifest": grading_manifest.model_dump(mode="json", by_alias=True),
            "grader_plan": grader.model_dump(mode="json", by_alias=True),
            "grader_resource_profile": resource.model_dump(mode="json", by_alias=True),
            "capabilities": [
                capability
                for capability in artifacts["capabilities"]
                if capability["delivery_phase"] == "grading"
            ],
        }
    )
    return execution, selection, grading, artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    execution, selection, grading, artifacts = vectors()
    rendered = {
        EXECUTION_PATH: _dump(execution),
        SELECTION_PATH: _dump(selection),
        GRADING_PATH: _dump(grading),
        ARTIFACT_PATH: _dump(artifacts),
    }
    if args.check:
        drift = [path for path, body in rendered.items() if path.read_text() != body]
        if drift:
            print("execution delivery vectors are stale: " + ", ".join(map(str, drift)))
            return 1
        return 0
    for path, body in rendered.items():
        path.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
