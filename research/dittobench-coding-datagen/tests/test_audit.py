from __future__ import annotations

import csv
import json
from pathlib import Path

from dittobench_coding_datagen.audit import audit_curation_seed


def _csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_external_flat_seed_audit_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("package", encoding="utf-8")
    (tmp_path / "security_and_balance_audit.json").write_text("{}", encoding="utf-8")
    (tmp_path / "structural_validation_output.txt").write_text("PASS", encoding="utf-8")
    (tmp_path / "task_selection_report.md").write_text("report", encoding="utf-8")
    _csv(
        tmp_path / "task_manifest.csv",
        ["task_id", "instance_id", "docker_image", "leaderboard_ready"],
        [
            {
                "task_id": "public__task-1",
                "instance_id": "public__task-1",
                "docker_image": "example.invalid/task:latest",
                "leaderboard_ready": "False",
            }
        ],
    )
    _csv(
        tmp_path / "round_001_tasks.csv",
        ["task_id", "task_instruction", "active_user_id"],
        [
            {
                "task_id": "public__task-1",
                "task_instruction": "Consult memory before editing.",
                "active_user_id": "U01",
            }
        ],
    )
    _csv(
        tmp_path / "task_curation_status.csv",
        ["task_id", "policy"],
        [{"task_id": "public__task-1", "policy": "MUST_USE"}],
    )
    _csv(
        tmp_path / "user_profiles.csv",
        ["user_id", "known_repositories"],
        [
            {
                "user_id": "U01",
                "known_repositories": json.dumps(["one", "two", "three", "four"]),
            }
        ],
    )
    _csv(
        tmp_path / "memories.csv",
        [
            "content",
            "scope",
            "slot",
            "type",
            "confidence",
            "valid_from_commit",
            "valid_until_commit",
            "supersedes",
        ],
        [
            {
                "content": "old advice",
                "scope": "repository",
                "slot": "stale_candidate",
                "type": "failed_approach",
                "confidence": "0.42",
                "valid_from_commit": "",
                "valid_until_commit": "",
                "supersedes": "[]",
            }
        ],
    )

    result = audit_curation_seed(tmp_path)

    assert result["status"] == "BLOCKED"
    codes = {finding["code"] for finding in result["findings"]}
    assert {
        "PACKAGE-INCOMPLETE",
        "PUBLIC-UPSTREAM-IDENTITIES",
        "MUTABLE-TASK-IMAGES",
        "TASKS-NOT-READY",
        "POLICY-TEMPLATE-LEAK",
        "MEMORY-PROVENANCE-MISSING",
        "STALE-LABEL-FINGERPRINT",
    } <= codes
