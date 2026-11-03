"""Regression tests for the judge-study scripts.

Run from this directory: python3 -I -m pytest test_judge_study_scripts.py
No network and no OPENROUTER_API_KEY are needed.
"""
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_run_judges():
    spec = importlib.util.spec_from_file_location(
        "run_judges", os.path.join(HERE, "run_judges.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stratified_limit_is_a_cap():
    rj = load_run_judges()
    probes = [{"probe": "P%d" % c, "probe_id": "%d-%d" % (c, i)}
              for c in range(5) for i in range(3)]
    sel = rj.select_probes(probes, 2, True)
    assert len(sel) == 2
    assert [p["probe"] for p in sel] == ["P0", "P1"]
    # Unchanged when the limit covers every category.
    assert len(rj.select_probes(probes, 10, True)) == 10
    assert rj.select_probes(probes, 0, True) == probes
    assert rj.select_probes([], 2, True) == []


def test_error_records_are_retried_on_resume(tmp_path):
    rj = load_run_judges()
    out = tmp_path / "out.jsonl"
    rows = [
        {"model_arm": "a", "probe_id": "ok", "rep": 0, "label": True, "raw": "yes"},
        {"model_arm": "a", "probe_id": "bad", "rep": 0, "error": "http429:x"},
    ]
    out.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    done = rj.load_done(str(out))
    assert ("a", "ok", 0) in done
    assert ("a", "bad", 0) not in done


def test_analyze_excludes_probes_short_of_reps(tmp_path):
    # 1,800 complete probes plus one probe whose third call errored and whose
    # two labelled calls disagree. It must not count as a 3-replicate result.
    path = tmp_path / "full.jsonl"
    with open(path, "w") as fh:
        for i in range(1800):
            for rep in range(3):
                fh.write(json.dumps({
                    "model_arm": "m@arm", "probe_id": "p%d" % i, "probe": "P6_wrong",
                    "question_type": "multi-session", "gold": False, "rep": rep,
                    "raw": "no", "label": False, "finish": "stop"}) + "\n")
        for rep, label in ((0, True), (1, False)):
            fh.write(json.dumps({
                "model_arm": "m@arm", "probe_id": "short", "probe": "P6_wrong",
                "question_type": "multi-session", "gold": False, "rep": rep,
                "raw": "yes" if label else "no", "label": label,
                "finish": "stop"}) + "\n")
        fh.write(json.dumps({
            "model_arm": "m@arm", "probe_id": "short", "probe": "P6_wrong",
            "question_type": "multi-session", "gold": False, "rep": 2,
            "error": "http502:x"}) + "\n")
    res = subprocess.run([sys.executable, "-I", os.path.join(HERE, "analyze.py"), str(path)],
                         capture_output=True, text=True)
    stability = [l for l in res.stdout.splitlines() if l.startswith("m@arm") and "/18" in l]
    assert stability, res.stdout + res.stderr
    assert "0/1800" in stability[0], stability[0]
