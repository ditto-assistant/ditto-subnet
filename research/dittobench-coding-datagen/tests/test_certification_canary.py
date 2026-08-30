from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.validation import validate_pack

ROOT = Path(__file__).parents[1]
PACK = ROOT / "practice" / "v1"
MANIFEST = ROOT / "certification" / "v1" / "manifest.json"
LOCKED_POLICY = (
    ROOT.parents[1]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_inference_policy_locked_v1.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_public_certification_canary_is_canonical_and_pack_bound() -> None:
    body = MANIFEST.read_bytes()
    manifest = json.loads(body)
    assert body == canonical_json_bytes(manifest)
    assert manifest["schema"] == "dittobench-coding-public-certification-canary-v1"
    assert manifest["coding_contract_version"] == 1
    assert manifest["corpus_scope"] == "public_certification"
    assert manifest["weight_eligible"] is False

    pack = validate_pack(PACK)
    assert manifest["practice_pack"] == {
        "agent_tasks_sha256": _sha256(PACK / "agent" / "tasks.jsonl"),
        "manifest_sha256": _sha256(PACK / "manifest.json"),
        "practice_pack_id": pack["practice_pack_id"],
    }
    assert manifest["inference_policy"]["sha256"] == _sha256(LOCKED_POLICY)


def test_public_certification_canary_uses_one_public_ledger_task() -> None:
    manifest = json.loads(MANIFEST.read_text())
    agent_task = next(
        json.loads(line)
        for line in (PACK / "agent" / "tasks.jsonl").read_text().splitlines()
        if json.loads(line)["task_id"] == "PRACTICE-LEDGER-001"
    )
    grader_task = next(
        json.loads(line)
        for line in (PACK / "grader" / "tasks.jsonl").read_text().splitlines()
        if json.loads(line)["task_id"] == "PRACTICE-LEDGER-001"
    )
    assert manifest["canary_id"] == "PUBLIC-CERTIFICATION-LEDGER-001"
    assert manifest["runner_plan"] == {
        "build_command_ids": agent_task["runtime_policy"]["build_command_ids"],
        "editable_paths": agent_task["runtime_policy"]["editable_paths"],
        "task_id": agent_task["task_id"],
        "test_command_ids": agent_task["runtime_policy"]["test_command_ids"],
    }
    assert manifest["grader_plan"]["task_id"] == grader_task["task_id"]
    assert manifest["grader_plan"]["grader_files"] == grader_task["grader_files"]
