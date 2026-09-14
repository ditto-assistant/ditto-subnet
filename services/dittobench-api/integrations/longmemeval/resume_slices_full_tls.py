#!/usr/bin/env python3
"""Single native resume of exactly four unjudged TLS failures; dry run default.

Preserves496 original judged rows, including wrong answers. Native resume also
regenerates the one answer whose judge failed (not a judged wrong-answer retry).
Original answer and all charged calls remain in original evidence/cost union.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from resume_fixed_backend_tls import require, digest, exclusive_bytes, encoded

SOURCE = "dfcb9498ca508d2f759ef83dd7c0c3d3512526a5"
BINARY = "e9ddf21cc9cfb3e24b53c77a14e27aa78fd3bdf20c6b1d596ce84ceadc092ae4"
CONDITION = "87d301eaa353a98ae327b3e6bbd19ae84f6646d8e0dcaf242397bc496a02691b"
PREPARED = "8af521b88d155c4c6e81befe89495b7e74328f65a734b91fdd45348a1a8a8023"
READER_FAILED = {"gpt4_e414231e", "2ebe6c92", "gpt4_68e94287"}
JUDGE_FAILED = "gpt4_7ca326fa"
FAILED = READER_FAILED | {JUDGE_FAILED}
ORIGINAL_RUN = "20260914-162449-" + SOURCE + "-bcc925de-ff4b-4549-b287-d2b79f1944e0-longmemeval.json"
INPUTS = {"runs/" + ORIGINAL_RUN: "e7ca1c7ed86448742407def1329a2616dbea65d3f424bf9a2a311dd5ae443d06",
          "checkpoint.jsonl": "982e88a2ead2e366c5fa144c1ecb86ff0ae1ae473498b16fad7b9336af39fa4c",
          "checkpoint.jsonl.provider-usage.jsonl": "e18671d74d7ad4a5718cea6a4a6000e818990c44cbbfb1ce65c6619424891fb6"}


def validate_original(report, rows, cohort):
    indexed = {r["case_id"]: r for r in report["per_case"]}
    require(len(indexed) == len(report["per_case"]) == len(cohort) == 500 and set(indexed) == set(cohort), "original coverage differs")
    require(len(rows) == len({r["case_id"] for r in rows}) == 496, "requires496 unique judged rows")
    require(set(cohort)-{r["case_id"] for r in rows} == FAILED, "retry complement differs")
    for row in rows:
        require(row == indexed[row["case_id"]], "checkpoint row altered")
        require(type(row.get("lme_correct")) is bool and row["model"] == "openai/gpt-5.6-luna", "unjudged/wrong model row")
        require(row["data"].get("query_status") == row["data"].get("judge_status") == "ok", "failed checkpoint row")
        require(row["data"].get("condition_sha256") == CONDITION, "wrong condition")
    for cid in FAILED:
        row = indexed[cid]
        require(row.get("lme_correct") is None, "cannot retry judged answer")
        if cid in READER_FAILED:
            require(row.get("notes") == ["agent loop: remote error: tls: bad record MAC"] and row["data"].get("query_status") == "error", "not exact reader TLS failure")
        else:
            require(row["data"].get("query_status") == "ok" and row["data"].get("judge_status") == "error" and
                    row["data"].get("rationale") == "judge completion: error reading response body: remote error: tls: bad record MAC", "not exact judge TLS failure")
    meta = report["meta"]
    require(meta["lme_complete"] == "false" and meta["lme_judged_cases"] == "496", "unexpected original status")
    require(meta["lme_prepared_snapshot_sha256"] == meta["lme_prepared_snapshot_after_sha256"] == PREPARED, "fixture changed")
    require(meta["lme_subject_graph_failures"] == "0" and meta["lme_subject_graph_discovery_complete"] == "true" and meta["lme_hydration_preflight_users"] == "500", "original execution gates failed")


def prepare(spec):
    require(spec["source_sha"] == SOURCE and spec["binary_sha256"] == BINARY and spec["arm"] == "summary", "wrong source/binary/arm")
    backend = Path(spec["backend"]).resolve()
    require(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=backend, text=True).strip() == SOURCE, "source changed")
    path = backend/"scripts/benchmarks/lme_slices_full.py"
    import sys
    sys.path.insert(0, str(path.parent))
    module_spec = importlib.util.spec_from_file_location("frozen_full", path)
    launcher = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(launcher)
    backend, command, runtime = launcher.verify(spec)
    retry_root = backend/".tmp/lme-slices-full/summary-retry4"
    for key in ("output", "log", "checkpoint", "cost_receipt", "launch_receipt"):
        require(Path(spec[key]).resolve().parent == retry_root, "outputs outside reviewed retry4 directory")
    original = backend/".tmp/lme-slices-full/summary"
    raw = {name: (original/name).read_bytes() for name in INPUTS}
    require(all(digest(raw[name]) == sha for name, sha in INPUTS.items()), "original artifact drift")
    report = json.loads(raw["runs/"+ORIGINAL_RUN])
    lines = raw["checkpoint.jsonl"].splitlines(keepends=True)
    require(len(lines) == 497, "original checkpoint must contain496 judged plus1 failed-judge row")
    excluded = [json.loads(line) for line in lines if json.loads(line)["case_id"] == JUDGE_FAILED]
    require(excluded == [row for row in report["per_case"] if row["case_id"] == JUDGE_FAILED], "failed-judge checkpoint row changed")
    checkpoint = b"".join(line for line in lines if json.loads(line)["case_id"] != JUDGE_FAILED)
    validate_original(report, [json.loads(line) for line in checkpoint.splitlines()], spec["case_ids"])
    claim = backend/".tmp/lme-slices-full/summary-retry4.claim.json"
    require(not claim.exists(), "single retry already claimed")
    return backend, command, runtime, checkpoint, claim


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    backend, command, runtime, checkpoint, claim = prepare(spec)
    if not args.execute:
        print("Verified496 unchanged judged rows and exact4 TLS-only retry complement; dry run only.")
        return
    wrapper_root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).parent, text=True).strip()
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=wrapper_root, text=True).strip(), "wrapper must be committed clean")
    receipt = {"schema": "lme-full-slices-tls-resume-v1", "started_at": datetime.now(timezone.utc).isoformat(),
               "source_sha": SOURCE, "binary_sha256": BINARY, "condition_sha256": CONDITION,
               "spec_sha256": digest(args.spec.read_bytes()), "original_sha256": INPUTS, "copied_rows": 496,
               "retry_case_ids": sorted(FAILED), "argv": command,
               "copied_checkpoint_sha256": digest(checkpoint),
               "scope": "One native resume, including regeneration of the one unjudged answer after judge TLS failure. Union all original and new cost receipts. Native retrieval preparation repeats500; embedding cost unknown. No seed/dream calls."}
    Path(spec["checkpoint"]).parent.mkdir(parents=True, exist_ok=True)
    exclusive_bytes(claim, encoded(receipt))
    exclusive_bytes(spec["launch_receipt"], encoded(receipt))
    exclusive_bytes(spec["checkpoint"], checkpoint)
    descriptor = os.open(spec["log"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.dup2(descriptor, 1)
    os.dup2(descriptor, 2)
    os.close(descriptor)
    os.chdir(backend)
    os.execve(command[0], command, runtime)


if __name__ == "__main__":
    main()
