from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_public_staging import _intake

from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.public_pack_v2 import compile_public_v2_pack
from dittobench_coding_datagen.public_result_runner import aggregate_public_v2_results


def _pack(root: Path) -> Path:
    intake = _intake(root / "staging")
    output = root / "pack"
    compile_public_v2_pack(
        staging_root=intake.parent, intake_path=intake, output=output
    )
    return output


def _task_results(root: Path, pack: Path) -> tuple[Path, ...]:
    index = [
        json.loads(line)
        for line in (pack / "tasks" / "index.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    paths: list[Path] = []
    for position, task in enumerate(index):
        body = canonical_json_bytes(
            {
                "condition": task["condition"],
                "patch_valid": True,
                "protocol_valid": True,
                "resolved": position % 2 == 0,
                "task_id": task["task_id"],
                "terminal_domain": "resolved"
                if position % 2 == 0
                else "repair_failure",
            }
        )
        path = root / f"result-{position}.json"
        path.write_bytes(body)
        paths.append(path)
    return tuple(paths)


def test_aggregate_public_results_emits_one_non_authoritative_report(
    tmp_path: Path,
) -> None:
    pack = _pack(tmp_path)
    result = aggregate_public_v2_results(
        pack=pack,
        harness_artifact_sha256="a" * 64,
        task_result_paths=_task_results(tmp_path, pack),
    )
    decoded = json.loads(result)

    assert decoded["tasks_total"] == 10
    assert decoded["resolved_count"] == 5
    assert decoded["authoritative"] is False
    assert decoded["leaderboard_eligible"] is False
    assert decoded["reward_eligible"] is False


def test_aggregate_public_results_rejects_condition_drift(tmp_path: Path) -> None:
    pack = _pack(tmp_path)
    results = list(_task_results(tmp_path, pack))
    body = json.loads(results[0].read_bytes())
    body["condition"] = "v4_current_override"
    results[0].write_bytes(canonical_json_bytes(body))
    with pytest.raises(CorpusError, match="authority"):
        aggregate_public_v2_results(
            pack=pack,
            harness_artifact_sha256="a" * 64,
            task_result_paths=tuple(results),
        )
