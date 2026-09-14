#!/usr/bin/env python3
"""One approved native resume of the frozen c274 full-500 TLS-failed campaign.

Dry-run by default. Preserves original artifacts, copies exactly495 successful
rows exclusively, and creates a NEW provider journal. No backend source changes.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

SOURCE = "c274a3f96e8176577043de95b048f1f2f3e9272d"
BINARY = "09b02ebb47094eff7729366920c5aa84fcff80acdd655cc1aef413584894e332"
PREPARED = "8af521b88d155c4c6e81befe89495b7e74328f65a734b91fdd45348a1a8a8023"
CONDITION = "5b5c15226d6c2a81323d44948c40bf395f2e0c20f4b6d392a0c7cc51fd40a4c1"
MODEL = "openai/gpt-5.6-luna"
TLS_ERROR = "agent loop: remote error: tls: bad record MAC"
FAILED = {"71017276", "gpt4_e072b769", "gpt4_4929293a", "af082822", "0db4c65d"}
INPUTS = {
    "qa-corrected-on-runs/20260914-132613-" + SOURCE + "-longmemeval.json":
        "b0a07a41b228f58710a17936bd93fd7635bb87e8f6dff22bd2e55da23e15db21",
    "qa-corrected-on-checkpoint.jsonl":
        "5097be1ba179d1e251017a4e3f03335e22147fa14429ab832621ac2b7343904a",
    "qa-corrected-on-checkpoint.jsonl.provider-usage.jsonl":
        "fc12e812d6df8d2333c65e9ddc7d3cb87fd072bc470bc86d4e5ba8a07c13f42a",
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_rows(report, rows, cohort):
    """Content-semantic guards independent of the fixed whole-file hash pins."""
    require(len(cohort) == 500 and len(rows) == 495, "requires500 cohort and495 checkpoint rows")
    cases = report["per_case"]
    require(len(cases) == 500, "first invocation must contain500 result rows")
    by_id = {row["case_id"]: row for row in cases}
    require(len(by_id) == 500 and set(by_id) == cohort, "report case coverage differs")
    resumed = {row["case_id"] for row in rows}
    require(len(resumed) == 495 and cohort - resumed == FAILED, "duplicate checkpoint or non-TLS retry complement")
    for row in rows:
        require(row == by_id[row["case_id"]], "checkpoint row differs from original result")
        data = row["data"]
        require(row["model"] == MODEL and isinstance(row.get("lme_correct"), bool), "unjudged/wrong-model checkpoint")
        require(data.get("query_status") == data.get("judge_status") == "ok", "failed checkpoint row")
        require(data.get("condition_sha256") == CONDITION, "checkpoint condition differs")
        require(data.get("seed_pair_count", 0) > 0 and data.get("graph_seed_pair_ids"), "missing hydrated graph seed context")
    for case_id in FAILED:
        row = by_id[case_id]
        require(row["model"] == MODEL and row.get("notes") == [TLS_ERROR], "retry not exact transport failure")
        require(row["data"].get("query_status") == "error" and row.get("lme_correct") is None, "retry case was already judged")
    meta = report["meta"]
    require(meta.get("lme_complete") == "false" and meta.get("lme_judged_cases") == "495", "unexpected original completeness")
    require(meta.get("lme_prepared_snapshot_sha256") == meta.get("lme_prepared_snapshot_after_sha256") == PREPARED, "prepared snapshot differs")
    require(meta.get("lme_subject_graph_discovery_complete") == "true" and meta.get("lme_subject_graph_failures") == "0", "original graph discovery failed")
    require(meta.get("lme_hydration_preflight_users") == "500", "original hydration coverage differs")


def exclusive_bytes(path, data):
    """Never overwrite originals, prior checkpoints, receipts, or retry claims."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def prepare(spec):
    require(spec.get("source_sha") == SOURCE and spec.get("binary_sha256") == BINARY, "requires exact frozen backend/binary")
    backend = Path(spec["backend"]).resolve()
    require(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=backend, text=True).strip() == SOURCE, "backend source moved")
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=backend, text=True).strip(), "backend source dirty")
    # Import only from the verified frozen owning checkout. Its existing verifier
    # guards binary VCS, assets, literal config, exactfull500 argv and fresh paths.
    module_spec = importlib.util.spec_from_file_location("frozen_campaign", backend / "scripts/benchmarks/lme_fixed_campaign.py")
    require(module_spec is not None and module_spec.loader is not None, "frozen verifier absent")
    launcher = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(launcher)
    backend, command, runtime = launcher.verify(spec)
    for key in ("output", "checkpoint", "cost_receipt", "log", "launch_receipt"):
        require(Path(spec[key]).name.startswith("qa-corrected-on-retry5-"), "requires distinct reviewed retry5 output names")
    taskdir = backend / ".tmp/lme-fixed"
    claim = taskdir / "qa-corrected-on-retry5.claim.json"
    require(not claim.exists(), "one retry invocation already claimed; no automatic retry")
    source_bytes = {}
    for relative, expected in INPUTS.items():
        source_bytes[relative] = (taskdir / relative).read_bytes()
        require(digest(source_bytes[relative]) == expected, "original artifact hash differs: " + relative)
    report = json.loads(source_bytes[next(iter(INPUTS))])
    checkpoint = source_bytes["qa-corrected-on-checkpoint.jsonl"]
    rows = [json.loads(line) for line in checkpoint.splitlines()]
    dataset = json.loads((Path(spec["dataset_root"]) / "longmemeval/longmemeval_s_cleaned.json").read_text())
    validate_rows(report, rows, {case["question_id"] for case in dataset})
    return backend, command, runtime, checkpoint, claim


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    backend, command, runtime, checkpoint, claim = prepare(spec)
    if not args.execute:
        print("Verified same frozen source/condition, immutable originals,495 valid rows and exact five TLS retries. Dry run only.")
        return
    wrapper_root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).parent, text=True).strip()
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=wrapper_root, text=True).strip(), "resume wrapper must be committed clean")
    wrapper_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=wrapper_root, text=True).strip()
    receipt = {
        "schema": "lme-native-tls-resume-v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "source_sha": SOURCE, "binary_sha256": BINARY, "wrapper_source_sha": wrapper_sha,
        "spec_sha256": digest(args.spec.read_bytes()), "original_artifacts_sha256": INPUTS,
        "copied_checkpoint_rows": 495, "retry_case_ids": sorted(FAILED), "argv": command,
        "prepared_snapshot_sha256": PREPARED, "condition_sha256": CONDITION,
        "cost_scope": "New journal contains only this invocation. Union original+retry generations for costs, including original failed calls. Native resume repeats500queryembedding/retrieval preparations; embedding cost unknown. No seed/dream/label replay.",
        "graph_audit_scope": "Each native report audits its own invocation. Preserve both; resumed495seedcontexts came from original invocation.",
        "max_retry_invocations": 1,
    }
    # Claim first; a crash after this point requires operator review, never a
    # silent second attempt. Fresh output paths were validated before copying.
    exclusive_bytes(claim, encoded(receipt))
    exclusive_bytes(spec["launch_receipt"], encoded(receipt))
    exclusive_bytes(spec["checkpoint"], checkpoint)
    require(digest(Path(spec["checkpoint"]).read_bytes()) == INPUTS["qa-corrected-on-checkpoint.jsonl"], "exclusive checkpoint copy differs")
    descriptor = os.open(spec["log"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.dup2(descriptor, 1)
    os.dup2(descriptor, 2)
    os.close(descriptor)
    os.chdir(backend)
    os.execve(command[0], command, runtime)


if __name__ == "__main__":
    main()
