"""The scorer's labels must equal the exporter's (contract §4.2)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from conftest import DATAGEN, EXPORTER

from cpm_scorer.labels import export_records
from cpm_scorer.tasks import build_tasks, memory_text, tool_text


def _load_exporter():
    if not EXPORTER.is_file():
        pytest.skip("docs/research/cpm/export_cpm.py not in this checkout")
    spec = importlib.util.spec_from_file_location("export_cpm", EXPORTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _exporter_rows(path: Path, tmp: Path) -> dict[str, list[dict]]:
    mod = _load_exporter()
    out = {}
    handles = {k: (tmp / f"{k}.jsonl").open("w") for k in ("pairs", "cases", "catalog")}
    try:
        mod.export_artifact(path, handles, Counter())
    finally:
        for h in handles.values():
            h.close()
    for k in handles:
        out[k] = [json.loads(line) for line in (tmp / f"{k}.jsonl").open()]
    return out


def _assert_parity(art_path: Path, tmp_path: Path) -> dict:
    ours = export_records(json.loads(art_path.read_text()), art_path.stem)
    theirs = _exporter_rows(art_path, tmp_path)
    # Compare through JSON, exactly as the exporter serialises its rows.
    for k in ("pairs", "cases", "catalog"):
        assert json.loads(json.dumps(ours[k], ensure_ascii=False)) == theirs[k], k
    return ours


def test_labels_match_exporter_on_fixture(tiny_artifact, tmp_path):
    art = tmp_path / "v18-5.json"
    art.write_text(json.dumps(tiny_artifact))
    ours = _assert_parity(art, tmp_path)
    by_id = {c["case_id"]: c for c in ours["cases"]}
    # The tool prerequisite pair tp1 is in the primary user's store (wave 0).
    assert by_id["m1"]["candidate_pair_ids"] == ["p2", "p1", "tp1", "p3"]
    assert by_id["m2"]["candidate_pair_ids"] == ["p2", "p1"]
    assert {s["type"] for s in by_id["m2"]["spans"]["p1"]} == {"distractor"}
    assert by_id["m3"]["decision"]["abstain"] is True
    assert by_id["t1"]["shortlist_target"] == ["send_poll", "get_group"]


@pytest.mark.skipif(
    not shutil.which("go") and not os.environ.get("CPM_GENERATE_BIN"),
    reason="needs the go toolchain or CPM_GENERATE_BIN",
)
def test_labels_match_exporter_on_real_seed(tmp_path):
    """Real `cmd/generate` v18 artifact (small profile keeps CI fast)."""
    bin_path = os.environ.get("CPM_GENERATE_BIN")
    if not bin_path:
        bin_path = str(tmp_path / "generate")
        subprocess.run(
            ["go", "build", "-o", bin_path, "./cmd/generate"], cwd=DATAGEN, check=True
        )
    art = tmp_path / "v18-42.json"
    subprocess.run(
        [bin_path, "-bench-version", "18", "-seed", "42", "-run-size", "small"]
        + ["-out", str(art)],
        check=True,
        capture_output=True,
    )
    ours = _assert_parity(art, tmp_path)
    tasks = build_tasks(ours)
    assert tasks.memory and tasks.tools
    assert all(t.relevant for t in tasks.memory)


def test_build_tasks_selection_and_order(tiny_artifact):
    rec = export_records(tiny_artifact, "v18-5")
    tasks = build_tasks(rec)
    # abstain (m3), chitchat (m4) and restraint (t0) cases are out of v1
    assert [t.case_id for t in tasks.memory] == ["m1", "m2"]
    assert [t.case_id for t in tasks.tools] == ["t1"]
    assert tasks.memory[0].candidates == ("p1", "p2", "p3", "tp1")
    assert tasks.tool_names == ("delete_all", "get_group", "send_poll")
    assert tasks.tool_text["send_poll"] == "send_poll: Send a poll."
    assert tasks.pair_text["p2"].startswith("[2025-01-01T00:00:00Z] User: My dentist")
    assert tasks.pair_text["p3"] == "User: \nAssistant: "
    limited = build_tasks(rec, limit_cases=1)
    assert [t.case_id for t in limited.memory] == ["m1"]


def test_candidate_text_formats():
    assert (
        memory_text({"timestamp": "T", "prompt": "a", "response": "b"})
        == "[T] User: a\nAssistant: b"
    )
    assert memory_text({"prompt": "a", "response": "b"}) == "User: a\nAssistant: b"
    assert tool_text({"name": "x", "description": "y"}) == "x: y"
