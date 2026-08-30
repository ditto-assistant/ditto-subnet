"""Tests for curator-only private Coding catalog publication plans."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ditto.api_models.coding_canonical import (
    coding_canonical_json_bytes,
    coding_canonical_sha256,
)
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import (
    CodingCatalogTaskPayload,
    coding_catalog_task_commitment_digest,
)
from ditto.api_server.coding_catalog_publication import (
    _MAX_PLAN_BYTES,
    CodingCatalogPublicationError,
    plan_private_catalog_publication,
    write_private_catalog_publication_plan,
)
from ditto.coding_selection import coding_catalog_leaf_hash

ROOT = Path(__file__).parents[5]
SELECTION = (
    ROOT / "packages/dittobench-coding-contract/testdata/coding_selection_v1.json"
)
EXECUTION = (
    ROOT / "packages/dittobench-coding-contract/testdata/coding_execution_plan_v1.json"
)


def _canonical(value: dict[str, Any], *, label: str) -> bytes:
    return coding_canonical_json_bytes(value, maximum_bytes=2 << 20, label=label)


def _fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    execution = json.loads(EXECUTION.read_text(encoding="utf-8"))
    payload = deepcopy(selection["task_version"]["payload"])
    payload["catalog_index"] = 0
    payload["task_version_id"] = "curator-private-task-v000"
    task_payload = CodingCatalogTaskPayload.model_validate(payload)
    task_commitment = coding_catalog_task_commitment_digest(task_payload)
    root = coding_catalog_leaf_hash(
        catalog_index=0,
        task_commitment_sha256=task_commitment,
    )
    commitment = deepcopy(selection["commitment"])
    commitment["catalog_merkle_root"] = root
    commitment["task_version_count"] = 1
    commitment.pop("commitment_sha256")
    commitment["commitment_sha256"] = coding_canonical_sha256(
        commitment,
        maximum_bytes=1 << 20,
        label="catalog commitment",
    )
    membership = {
        "schema": "dittobench-coding-catalog-membership-proof-v1",
        "coding_contract_version": 1,
        "corpus_release_id": commitment["corpus_release_id"],
        "catalog_merkle_root": root,
        "task_version_count": 1,
        "catalog_index": 0,
        "task_commitment_sha256": task_commitment,
        "sibling_sha256": [],
    }
    membership["catalog_membership_proof_sha256"] = coding_canonical_sha256(
        membership,
        maximum_bytes=1 << 20,
        label="catalog membership proof",
    )
    record = {
        "schema": "dittobench-coding-private-catalog-record-v1",
        "catalog_commitment_sha256": commitment["commitment_sha256"],
        "task_version": {
            "payload": payload,
            "task_commitment_sha256": task_commitment,
        },
        "membership_proof": membership,
        "issue": selection["issue"],
        "runtime_policy": selection["runtime_policy"],
        "budgets": selection["budgets"],
        "runner_plan": execution["runner_plan"],
        "grader_plan": execution["grader_plan"],
        "grader_resource_profile": execution["grader_resource_profile"],
    }
    CodingCatalogCommitment.model_validate(commitment)
    return commitment, record


def _write_fixture(root: Path) -> tuple[Path, Path]:
    commitment, record = _fixture()
    commitment_path = root / "commitment.json"
    records_dir = root / "records"
    records_dir.mkdir(parents=True)
    commitment_path.write_bytes(_canonical(commitment, label="catalog commitment"))
    (records_dir / "000000.json").write_bytes(
        _canonical(record, label="private catalog record")
    )
    return commitment_path, records_dir


def test_curator_publication_plan_is_canonical_and_content_addressed(
    tmp_path: Path,
) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    plan = plan_private_catalog_publication(
        commitment_path=commitment_path,
        records_dir=records_dir,
    )

    assert len(plan.objects) == 1
    item = plan.objects[0]
    assert item.catalog_index == 0
    assert item.object_key == (
        f"coding-catalog/v1/{plan.commitment.commitment_sha256}/records/000000.json"
    )
    output = write_private_catalog_publication_plan(
        plan=plan,
        output=tmp_path / "publication.json",
    )
    body = json.loads(output.read_bytes())
    assert body["publication_sha256"] == plan.publication_sha256
    assert body["objects"] == [item.as_json()]
    assert "problem_statement" not in output.read_text(encoding="utf-8")
    with pytest.raises(CodingCatalogPublicationError, match="digest"):
        write_private_catalog_publication_plan(
            plan=replace(plan, publication_sha256="f" * 64),
            output=tmp_path / "forged-publication.json",
        )


def test_curator_publication_rejects_noncanonical_or_incomplete_records(
    tmp_path: Path,
) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    record = records_dir / "000000.json"
    raw = json.loads(record.read_bytes())
    raw["future_private_hint"] = "must not become publication authority"
    record.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CodingCatalogPublicationError, match="not canonical"):
        plan_private_catalog_publication(
            commitment_path=commitment_path,
            records_dir=records_dir,
        )

    commitment_path, records_dir = _write_fixture(tmp_path / "incomplete")
    (records_dir / "000001.json").write_bytes(
        (records_dir / "000000.json").read_bytes()
    )
    with pytest.raises(
        CodingCatalogPublicationError, match="does not match the commitment"
    ):
        plan_private_catalog_publication(
            commitment_path=commitment_path,
            records_dir=records_dir,
        )

    commitment_path, records_dir = _write_fixture(tmp_path / "oversized")
    commitment_path.write_bytes(b"{" + (b"x" * ((1 << 20) + 1)) + b"}")
    with pytest.raises(CodingCatalogPublicationError, match="exceeds bounds"):
        plan_private_catalog_publication(
            commitment_path=commitment_path,
            records_dir=records_dir,
        )


def test_publication_plan_bound_covers_maximum_committed_catalog() -> None:
    assert _MAX_PLAN_BYTES >= (1_000_000 * 1024) + (1 << 20)
